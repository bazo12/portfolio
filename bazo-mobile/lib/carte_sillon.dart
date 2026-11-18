import 'dart:async';
import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'fmt.dart';
import 'glass.dart';
import 'texture.dart';
import 'theme.dart';

/// Géométrie de la carte : port de `dessinerCarte()` (bazo_web/app.js).
/// Les gares sont placées d'après leur latitude / longitude, reliées par des
/// courbes lisses ; chaque tronçon garde sa longueur pour y poser les trains.
class GeoCarte {
  GeoCarte(this.reseau, this.taille) {
    stations = ((reseau['stations'] as List?) ?? const []).cast<Map>().map((m) => m.cast<String, dynamic>()).toList();
    final n = stations.length;
    if (n < 2) {
      pts = const [];
      chemins = const [];
      metriques = const [];
      cumul = const [0];
      cy = taille.height * 0.52;
      return;
    }
    final lats = stations.map((s) => (s['lat'] as num).toDouble()).toList();
    final lons = stations.map((s) => (s['lon'] as num).toDouble()).toList();
    final latMin = lats.reduce(math.min), latMax = lats.reduce(math.max);
    final lonMin = lons.reduce(math.min), lonMax = lons.reduce(math.max);
    final lonMoy = (lonMin + lonMax) / 2;
    final lonAmp = (lonMax - lonMin) == 0 ? 1.0 : (lonMax - lonMin);
    final W = taille.width, H = taille.height;
    const mx = 34.0;
    cy = H * 0.52;
    final k = (H * 0.22) / lonAmp;
    final dLat = (latMax - latMin) == 0 ? 1.0 : (latMax - latMin);
    pts = [
      for (var i = 0; i < n; i++)
        Offset(
          mx + ((lats[i] - latMin) / dLat) * (W - 2 * mx),
          cy + (lons[i] - lonMoy) * k,
        ),
    ];
    final ch = <Path>[];
    for (var i = 0; i < n - 1; i++) {
      final p0 = pts[math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[math.min(n - 1, i + 2)];
      final c1 = Offset(p1.dx + (p2.dx - p0.dx) / 6, p1.dy + (p2.dy - p0.dy) / 6);
      final c2 = Offset(p2.dx - (p3.dx - p1.dx) / 6, p2.dy - (p3.dy - p1.dy) / 6);
      ch.add(Path()
        ..moveTo(p1.dx, p1.dy)
        ..cubicTo(c1.dx, c1.dy, c2.dx, c2.dy, p2.dx, p2.dy));
    }
    chemins = ch;
    metriques = ch.map((p) => p.computeMetrics().first).toList();
    final c = <double>[0];
    for (final m in metriques) {
      c.add(c.last + m.length);
    }
    cumul = c;
    idx = {for (var i = 0; i < n; i++) stations[i]['uic'] as String: i};
  }

  final Json reseau;
  final Size taille;
  late final List<Json> stations;
  late final List<Offset> pts;
  late final List<Path> chemins;
  late final List<ui.PathMetric> metriques;
  late final List<double> cumul;
  late final double cy;
  Map<String, int> idx = const {};

  bool get exploitable => pts.length >= 2;

  Offset pointSurTrace(double arc) {
    final total = cumul.last;
    final a = arc.clamp(0, total).toDouble();
    var k = 0;
    while (k < metriques.length - 1 && a > cumul[k + 1]) {
      k++;
    }
    final t = metriques[k].getTangentForOffset(math.max(0, a - cumul[k]));
    return t?.position ?? pts.first;
  }

  /// Port de `positionTrain()` : où est le train sur le tracé à l'instant `now` ?
  double? positionTrain(Json t, double now) {
    final stops = ((t['stops'] as List?) ?? const []).cast<List>();
    final arr = stops.where((s) => (s[3] as num) == 0 && idx.containsKey(s[0])).toList();
    if (arr.length < 2) return null;
    if (now < (arr.first[2] as num) - 20 || now > (arr.last[1] as num) + 45) return null;
    double arc(List s) => cumul[idx[s[0]]!];
    for (var i = 0; i < arr.length; i++) {
      final tin = (arr[i][1] as num).toDouble(), tout = (arr[i][2] as num).toDouble();
      if (now >= tin && now <= tout) return arc(arr[i]);
      if (i < arr.length - 1) {
        final tinSuiv = (arr[i + 1][1] as num).toDouble();
        if (now > tout && now < tinSuiv) {
          final f = (now - tout) / math.max(1, tinSuiv - tout);
          return arc(arr[i]) + (arc(arr[i + 1]) - arc(arr[i])) * f;
        }
      }
    }
    return arc(arr.first);
  }
}

class CarteSillon extends StatefulWidget {
  const CarteSillon({
    super.key,
    required this.live,
    required this.recuLe,
    this.horsLigne = false,
    this.margeHaut = 0,
  });

  /// Réponse de GET /v1/live/{id} (ou dernier instantané en cache).
  final Json? live;
  final DateTime recuLe;
  final bool horsLigne;
  final double margeHaut;

  @override
  State<CarteSillon> createState() => _CarteSillonState();
}

class _CarteSillonState extends State<CarteSillon> {
  Timer? _tic;
  Timer? _tipTimer;
  String? _tip;
  GeoCarte? _geo;
  Size? _geoTaille;
  Json? _geoReseau;

  @override
  void initState() {
    super.initState();
    _tic = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _tic?.cancel();
    _tipTimer?.cancel();
    super.dispose();
  }

  Json? get _reseau => (widget.live?['reseau'] as Map?)?.cast<String, dynamic>();

  double get _maintenantServeur {
    final r = _reseau;
    if (r == null) return DateTime.now().millisecondsSinceEpoch / 1000;
    final base = ((r['serveur_ts'] ?? r['generated_ts']) as num).toDouble();
    return base + DateTime.now().difference(widget.recuLe).inMilliseconds / 1000;
  }

  GeoCarte? _geometrie(Size taille) {
    final r = _reseau;
    if (r == null) return null;
    if (_geo == null || _geoTaille != taille || !identical(_geoReseau, r)) {
      _geo = GeoCarte(r, taille);
      _geoTaille = taille;
      _geoReseau = r;
    }
    return _geo;
  }

  void _toucher(TapUpDetails d, Size taille) {
    final geo = _geometrie(taille);
    final r = _reseau;
    if (geo == null || !geo.exploitable || r == null) return;
    final now = _maintenantServeur;
    String? meilleur;
    var dMin = 26.0;
    Json? train;
    for (final t in ((r['trains'] as List?) ?? const []).cast<Map>()) {
      final tj = t.cast<String, dynamic>();
      if (tj['statut'] == 'supprime') continue;
      final arc = geo.positionTrain(tj, now);
      if (arc == null) continue;
      final p = geo.pointSurTrace(arc);
      final dist = (p - d.localPosition).distance;
      if (dist < dMin) {
        dMin = dist;
        meilleur = tj['numero'] as String?;
        train = tj;
      }
    }
    if (meilleur == null || train == null) return;
    final stops = (train['stops'] as List).cast<List>();
    final dernier = stops.last[0];
    final nom = geo.stations.firstWhere((s) => s['uic'] == dernier, orElse: () => {})['nom'];
    final retard = (train['retard'] as num?)?.toInt() ?? 0;
    HapticFeedback.selectionClick();
    setState(() {
      _tip = 'Train $meilleur${nom != null ? ' → $nom' : ''} · ${retard >= 5 ? '+$retard min' : 'à l\'heure'}';
    });
    _tipTimer?.cancel();
    _tipTimer = Timer(const Duration(milliseconds: 3500), () {
      if (mounted) setState(() => _tip = null);
    });
  }

  String _ageTexte(double age) => age < 90 ? 'à jour · ${age.round()} s' : 'il y a ${(age / 60).round()} min';

  @override
  Widget build(BuildContext context) {
    final r = _reseau;
    final resume = (r?['resume'] as Map?)?.cast<String, dynamic>();
    final pire = resume?['statut'] as String? ?? 'fluide';
    final nbSuppr = (resume?['nb_supprimes'] as num?)?.toInt() ?? 0;
    final nbRet = (resume?['nb_retards'] as num?)?.toInt() ?? 0;
    final age = r == null ? 0.0 : math.max(0, _maintenantServeur - (r['generated_ts'] as num).toDouble());
    final periome = age > 600 || widget.horsLigne;

    String badge;
    String badgeStatut = pire;
    if (r == null) {
      badge = widget.horsLigne ? 'Hors ligne' : 'Temps réel en pause';
      badgeStatut = 'retard';
    } else if (pire == 'supprime') {
      badge = 'Perturbé · $nbSuppr suppression${nbSuppr > 1 ? 's' : ''}';
    } else if (pire == 'retard') {
      badge = 'Retards · $nbRet train${nbRet > 1 ? 's' : ''}';
    } else {
      badge = 'Trafic fluide';
    }

    final nt = (widget.live?['prochain_train'] as Map?)?.cast<String, dynamic>();

    return ClipRect(
      child: Stack(
        fit: StackFit.expand,
        children: [
          const DecoratedBox(
            decoration: BoxDecoration(
              gradient: RadialGradient(
                center: Alignment(-0.6, -1),
                radius: 1.3,
                colors: [Color(0xFF1B2A4A), Color(0xFF121C33)],
              ),
            ),
          ),
          const CustomPaint(painter: _ReliefPainter()),
          LayoutBuilder(
            builder: (context, c) {
              final taille = Size(c.maxWidth, c.maxHeight);
              final geo = _geometrie(taille);
              return GestureDetector(
                behavior: HitTestBehavior.opaque,
                onTapUp: (d) => _toucher(d, taille),
                child: CustomPaint(
                  size: taille,
                  painter: _CartePainter(
                    geo: geo,
                    maintenant: _maintenantServeur,
                    numeroMoi: nt?['numero'] as String?,
                    uicMoi: {if (nt?['uic_dep'] != null) nt!['uic_dep'] as String, if (nt?['uic_arr'] != null) nt!['uic_arr'] as String},
                    reseau: r,
                  ),
                ),
              );
            },
          ),
          const GrainOverlay(opacite: 0.12),
          Positioned(
            left: 18,
            top: widget.margeHaut + 10,
            child: Row(
              children: [
                Container(
                  width: 10,
                  height: 10,
                  decoration: const BoxDecoration(
                    shape: BoxShape.circle,
                    gradient: LinearGradient(colors: [Color(0xFFB4C4FF), Bz.ok]),
                    boxShadow: [BoxShadow(color: Color(0x8810B981), blurRadius: 10)],
                  ),
                ),
                const SizedBox(width: 8),
                const Text('Bazo', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17, letterSpacing: -0.3)),
              ],
            ),
          ),
          if (r != null)
            Positioned(
              right: 18,
              top: widget.margeHaut + 12,
              child: Text(
                widget.horsLigne ? 'hors ligne · ${_ageTexte(age.toDouble())}' : _ageTexte(age.toDouble()),
                style: Bz.chiffres.copyWith(
                  fontSize: 12.5,
                  fontWeight: FontWeight.w600,
                  color: periome ? Bz.ambre : Bz.encre2,
                ),
              ),
            ),
          Positioned(
            left: 16,
            bottom: 14,
            right: 16,
            child: Row(
              children: [
                PastilleStatut(statut: badgeStatut, texte: badge),
                const Spacer(),
                if (_tip != null)
                  Flexible(
                    child: GlassCard(
                      rayon: 14,
                      flou: 14,
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                      child: Text(_tip!, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Relief topographique très discret derrière le tracé.
class _ReliefPainter extends CustomPainter {
  const _ReliefPainter();

  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1
      ..color = Colors.white.withValues(alpha: 0.035);
    final centre = Offset(size.width * 0.72, size.height * 1.05);
    for (var i = 1; i <= 7; i++) {
      canvas.drawOval(
        Rect.fromCenter(center: centre, width: size.width * (0.35 + i * 0.22), height: size.height * (0.3 + i * 0.2)),
        p,
      );
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter old) => false;
}

class _CartePainter extends CustomPainter {
  _CartePainter({
    required this.geo,
    required this.maintenant,
    required this.numeroMoi,
    required this.uicMoi,
    required this.reseau,
  });

  final GeoCarte? geo;
  final double maintenant;
  final String? numeroMoi;
  final Set<String> uicMoi;
  final Json? reseau;

  static const _cles = {'Nancy', 'Metz', 'Thionville', 'Luxembourg'};

  void _texte(Canvas c, String t, Offset ancre, {required String align, required Color couleur, bool gras = true}) {
    final tp = TextPainter(
      text: TextSpan(
        text: t,
        style: TextStyle(color: couleur, fontSize: 11.5, fontWeight: gras ? FontWeight.w700 : FontWeight.w500, letterSpacing: 0.2),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    final x = align == 'start' ? ancre.dx : (align == 'end' ? ancre.dx - tp.width : ancre.dx - tp.width / 2);
    tp.paint(c, Offset(x, ancre.dy - tp.height / 2));
  }

  @override
  void paint(Canvas canvas, Size size) {
    final g = geo;
    final r = reseau;
    if (g == null || r == null || !g.exploitable) return;
    final segments = ((r['segments'] as List?) ?? const []).cast<Map>();

    for (var i = 0; i < g.chemins.length; i++) {
      final statut = i < segments.length ? segments[i]['statut'] as String? : 'fluide';
      final c = Bz.pourStatut(statut);
      canvas.drawPath(
        g.chemins[i],
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeCap = StrokeCap.round
          ..strokeWidth = 9
          ..color = c.withValues(alpha: 0.38)
          ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 4.5),
      );
    }
    for (var i = 0; i < g.chemins.length; i++) {
      final statut = i < segments.length ? segments[i]['statut'] as String? : 'fluide';
      canvas.drawPath(
        g.chemins[i],
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeCap = StrokeCap.round
          ..strokeWidth = 3.4
          ..color = Bz.pourStatut(statut),
      );
    }

    final n = g.stations.length;
    for (var i = 0; i < n; i++) {
      final s = g.stations[i];
      final p = g.pts[i];
      final moi = uicMoi.contains(s['uic']);
      canvas.drawCircle(p, moi ? 5.5 : 3.6, Paint()..color = moi ? Bz.indigo : Bz.fond);
      canvas.drawCircle(
        p,
        moi ? 5.5 : 3.6,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = moi ? 2 : 1.6
          ..color = moi ? Colors.white : Colors.white.withValues(alpha: 0.85),
      );
      final nom = s['nom'] as String;
      if (_cles.contains(nom) || moi) {
        final haut = i > 0 && i < n - 1 && p.dy > g.cy;
        final align = i == 0 ? 'start' : (i == n - 1 ? 'end' : 'middle');
        final x = i == 0 ? math.max(8.0, p.dx - 6) : (i == n - 1 ? math.min(size.width - 8, p.dx + 6) : p.dx);
        _texte(canvas, nom, Offset(x, p.dy + (haut ? -18 : 21)), align: align, couleur: moi ? Colors.white : Bz.encre2);
      }
    }

    for (final t in ((r['trains'] as List?) ?? const []).cast<Map>()) {
      final tj = t.cast<String, dynamic>();
      if (tj['statut'] == 'supprime') continue;
      final arc = g.positionTrain(tj, maintenant);
      if (arc == null) continue;
      final p = g.pointSurTrace(arc);
      final couleur = tj['statut'] == 'retard' ? Bz.trainRetard : Bz.trainOk;
      canvas.drawCircle(p, 8, Paint()..color = couleur.withValues(alpha: 0.30)..maskFilter = const MaskFilter.blur(BlurStyle.normal, 3));
      canvas.drawCircle(p, 3.8, Paint()..color = couleur);
      if (numeroMoi != null && tj['numero'] == numeroMoi) {
        canvas.drawCircle(
          p,
          9,
          Paint()
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.8
            ..color = Colors.white,
        );
      }
    }
  }

  @override
  bool shouldRepaint(_CartePainter old) => true;
}
