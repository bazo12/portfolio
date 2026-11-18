import 'dart:async';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'actions.dart';
import 'bazo_api.dart';
import 'carte_sillon.dart';
import 'fmt.dart';
import 'glass.dart';
import 'live_cache.dart';
import 'texture.dart';
import 'theme.dart';

/// Écran d'accueil : port de `chargerAccueil()` / `dessinerHero()` de bazo_web/app.js.
/// Carte du Sillon en haut, tiroir qui glisse sur trois crans (22 / 35 / 52 %).
class AccueilScreen extends StatefulWidget {
  const AccueilScreen({
    super.key,
    required this.api,
    required this.actif,
    required this.onAller,
    required this.onPending,
  });

  final BazoApi api;
  final bool actif;
  final ValueChanged<int> onAller;
  final ValueChanged<bool> onPending;

  @override
  State<AccueilScreen> createState() => _AccueilScreenState();
}

class _AccueilScreenState extends State<AccueilScreen> with WidgetsBindingObserver {
  static const _crans = [0.22, 0.35, 0.52];
  static const _raisons = {
    'trajet_hors_sillon':
        'Ton trajet n\'est pas encore couvert par la carte en direct — tes alertes et ton attestation fonctionnent normalement.',
    'aucun_train': 'Plus de train prévu sur ton trajet pour l\'instant.',
    'donnees_indisponibles': 'Données temps réel indisponibles pour le moment.',
    'temps_reel_indisponible': 'Le suivi en direct reprend dans quelques minutes.',
  };

  Json? _live;
  DateTime _recuLe = DateTime.now();
  bool _horsLigne = false;
  Json? _pending;
  Json? _statut;
  Json? _greve;
  String? _erreur;
  bool _premierChargement = true;

  double _fraction = 0.35;
  bool _glisse = false;
  Timer? _rafraichir;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _charger();
    _rafraichir = Timer.periodic(const Duration(seconds: 30), (_) {
      if (widget.actif && mounted) _charger(silencieux: true);
    });
  }

  @override
  void didUpdateWidget(AccueilScreen old) {
    super.didUpdateWidget(old);
    if (widget.actif && !old.actif) _charger(silencieux: true);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState etat) {
    if (etat == AppLifecycleState.resumed && widget.actif) _charger(silencieux: true);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _rafraichir?.cancel();
    super.dispose();
  }

  Future<void> _charger({bool silencieux = false}) async {
    try {
      final r = await Future.wait<Object?>([
        widget.api.liveAccueil(),
        widget.api.pending().then<Object?>((v) => v).catchError((_) => null),
        widget.api.statutMois().then<Object?>((v) => v).catchError((_) => null),
      ]);
      if (!mounted) return;
      final live = r[0] as Json;
      setState(() {
        _live = live;
        _recuLe = DateTime.now();
        _horsLigne = false;
        _pending = r[1] as Json?;
        _statut = r[2] as Json?;
        _erreur = null;
        _premierChargement = false;
      });
      widget.onPending(n(_pending?['nb_pending']) > 0);
      LiveCache.ecrire(live);
      _chargerGreve();
    } on ApiException catch (e) {
      await _echec(e.message, silencieux);
    } catch (e) {
      await _echec('Impossible de charger l\'accueil.', silencieux);
    }
  }

  Future<void> _echec(String message, bool silencieux) async {
    if (!mounted) return;
    if (_live == null) {
      final cache = await LiveCache.lire();
      if (!mounted) return;
      if (cache != null) {
        setState(() {
          _live = cache.live;
          _recuLe = cache.quand;
          _horsLigne = true;
        });
      }
    } else {
      setState(() => _horsLigne = true);
    }
    setState(() {
      _erreur = message;
      _premierChargement = false;
    });
    if (!silencieux && mounted) toast(context, message, erreur: true);
  }

  Future<void> _chargerGreve() async {
    try {
      final g = await widget.api.greve();
      if (mounted) setState(() => _greve = g);
    } catch (_) {
      if (mounted) setState(() => _greve = null);
    }
  }

  // ------------------------------------------------------------ tiroir

  double _cranProche(double v) => _crans.reduce((a, b) => (b - v).abs() < (a - v).abs() ? b : a);

  void _glisser(DragUpdateDetails d, double hauteur) {
    setState(() {
      _glisse = true;
      _fraction = (_fraction + d.delta.dy / hauteur).clamp(_crans.first, _crans.last);
    });
  }

  void _relacher() {
    HapticFeedback.selectionClick();
    setState(() {
      _glisse = false;
      _fraction = _cranProche(_fraction);
    });
  }

  void _cran(int sens) {
    final i = _crans.indexOf(_cranProche(_fraction));
    final j = (i + sens).clamp(0, _crans.length - 1);
    HapticFeedback.selectionClick();
    setState(() {
      _glisse = false;
      _fraction = _crans[j];
    });
  }

  // ------------------------------------------------------------ rendu

  @override
  Widget build(BuildContext context) {
    final mq = MediaQuery.of(context);
    final hauteur = mq.size.height;
    final carteH = _fraction * hauteur;
    final duree = _glisse ? Duration.zero : const Duration(milliseconds: 320);

    return Stack(
      children: [
        AnimatedPositioned(
          duration: duree,
          curve: Curves.easeOutCubic,
          left: 0,
          right: 0,
          top: 0,
          height: carteH + 30,
          child: CarteSillon(
            live: _live,
            recuLe: _recuLe,
            horsLigne: _horsLigne,
            margeHaut: mq.padding.top,
          ),
        ),
        AnimatedPositioned(
          duration: duree,
          curve: Curves.easeOutCubic,
          left: 0,
          right: 0,
          top: carteH - 22,
          bottom: 0,
          child: _tiroir(context, hauteur),
        ),
      ],
    );
  }

  Widget _tiroir(BuildContext context, double hauteur) {
    final bas = MediaQuery.of(context).padding.bottom + 108;
    return ClipRRect(
      borderRadius: const BorderRadius.vertical(top: Radius.circular(30)),
      child: BackdropFilter(
        filter: ui.ImageFilter.blur(sigmaX: 28, sigmaY: 28),
        child: GrainOverlay(
          opacite: 0.07,
          enfant: Container(
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: [
                  const Color(0xFF1A2440).withValues(alpha: 0.78),
                  const Color(0xFF0F172A).withValues(alpha: 0.88),
                ],
              ),
              border: const Border(top: BorderSide(color: Bz.bordVerre)),
            ),
            child: Column(
              children: [
                Semantics(
                  slider: true,
                  label: 'Hauteur de la carte',
                  value: '${(_fraction * 100).round()} %',
                  onIncrease: () => _cran(1),
                  onDecrease: () => _cran(-1),
                  child: GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onVerticalDragUpdate: (d) => _glisser(d, hauteur),
                    onVerticalDragEnd: (_) => _relacher(),
                    onVerticalDragCancel: _relacher,
                    child: SizedBox(
                      height: 30,
                      width: double.infinity,
                      child: Center(
                        child: Container(
                          width: 44,
                          height: 5,
                          decoration: BoxDecoration(
                            color: Colors.white.withValues(alpha: _glisse ? 0.5 : 0.28),
                            borderRadius: BorderRadius.circular(99),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                Expanded(
                  child: RefreshIndicator(
                    color: Bz.indigo,
                    backgroundColor: const Color(0xFF1A2440),
                    onRefresh: _charger,
                    child: ListView(
                      physics: const AlwaysScrollableScrollPhysics(parent: BouncingScrollPhysics()),
                      padding: EdgeInsets.fromLTRB(16, 2, 16, bas),
                      children: _contenu(),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  List<Widget> _contenu() {
    final live = _live;
    if (live == null) {
      if (_premierChargement) return const [Squelette(lignes: 4)];
      return [
        GlassCard(
          child: VideBz(titre: 'Accueil indisponible', texte: _erreur ?? 'Tire vers le bas pour réessayer.'),
        ),
      ];
    }
    final nt = live['prochain_train'] is Map ? m(live['prochain_train']) : null;
    return [
      if (_horsLigne) _bandeauHorsLigne(),
      _bandeauCheckout(),
      _hero(live, nt),
      if (nt != null && lm(nt['alternatives']).isNotEmpty) ...[
        const SizedBox(height: 14),
        _listeTrains('Alternatives qui circulent', lm(nt['alternatives']), avecTag: true),
      ],
      ..._suivants(nt),
      ..._carteGreve(),
      if (_statut != null) ...[const SizedBox(height: 14), _stats(_statut!)],
    ];
  }

  Widget _bandeauHorsLigne() {
    final age = DateTime.now().difference(_recuLe);
    final quand = age.inMinutes < 1
        ? 'à l\'instant'
        : age.inHours < 1
            ? 'il y a ${age.inMinutes} min'
            : 'du ${_recuLe.day.toString().padLeft(2, '0')}/${_recuLe.month.toString().padLeft(2, '0')} à ${_recuLe.hour.toString().padLeft(2, '0')}h${_recuLe.minute.toString().padLeft(2, '0')}';
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: GlassCard(
        teinte: Bz.ambre,
        rayon: 16,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 11),
        child: Row(
          children: [
            const Icon(Icons.cloud_off_rounded, color: Bz.ambre, size: 19),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                'Hors ligne — données $quand. Elles peuvent avoir changé.',
                style: const TextStyle(fontSize: 13.5, color: Bz.encre),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _bandeauCheckout() {
    final nb = n(_pending?['nb_pending']);
    if (nb == 0) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: GlassCard(
        teinte: Bz.indigo,
        rayon: 16,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        onTap: () => widget.onAller(1),
        child: Row(
          children: [
            Expanded(
              child: Text(
                '${nb == 1 ? 'Un trajet attend ta confirmation' : '$nb trajets attendent ta confirmation'} — comment s\'est passé ton train ?',
                style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 14.5),
              ),
            ),
            const Icon(Icons.chevron_right_rounded, color: Bz.encre2),
          ],
        ),
      ),
    );
  }

  Widget _hero(Json live, Json? nt) {
    final profil = m(live['profil']);
    final pdf = m(live['pdf']);
    final pdfOk = pdf['disponible'] == true;

    String eyebrow, route, heure = '—', plan = '', sousTexte = '';
    String? statut;
    String etiquette = '';
    Widget? sousRiche;

    if (nt == null) {
      eyebrow = 'Mon prochain train';
      route = s(profil['gare_depart']).isNotEmpty ? '${s(profil['gare_depart'])} ⇄ ${s(profil['gare_retour'])}' : 'Mon trajet';
      sousTexte = _raisons[s(live['raison'])] ?? 'Aucun train à afficher.';
    } else {
      statut = s(nt['statut'], 'fluide');
      eyebrow = (nt['habituel'] == true ? 'Mon train habituel' : 'Mon prochain train') + (nt['en_cours'] == true ? ' · en route' : '');
      route = '${s(nt['depart_gare'])} → ${s(nt['arrivee_gare'])}';
      heure = statut == 'supprime' ? s(nt['depart_plan']) : s(nt['depart_reel']);
      if (statut != 'supprime' && n(nt['retard_depart']) >= 1) plan = s(nt['depart_plan']);
      etiquette = s(nt['etiquette']);
      const base = TextStyle(color: Bz.encre2, fontSize: 14.5, height: 1.4);
      final gras = base.copyWith(color: Bz.encre, fontWeight: FontWeight.w700);
      if (statut == 'supprime') {
        sousRiche = Text.rich(
          TextSpan(style: base, children: [
            const TextSpan(text: 'Train '),
            TextSpan(text: s(nt['numero']), style: gras),
            const TextSpan(text: ' · ce train est '),
            TextSpan(text: 'supprimé', style: gras),
            const TextSpan(text: ' sur ton trajet — regarde les alternatives ci-dessous.'),
          ]),
        );
      } else {
        sousRiche = Text.rich(
          TextSpan(style: base, children: [
            const TextSpan(text: 'Train '),
            TextSpan(text: s(nt['numero']), style: gras),
            TextSpan(text: ' · arrivée à ${s(nt['arrivee_gare'])} '),
            TextSpan(text: s(nt['arrivee_reelle']), style: gras),
            if (n(nt['retard_arrivee']) >= 1) TextSpan(text: '  +${n(nt['retard_arrivee'])} min', style: gras.copyWith(color: Bz.ambre)),
          ]),
        );
      }
    }

    return GlassCard(
      fort: true,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            eyebrow.toUpperCase(),
            style: const TextStyle(color: Bz.encre3, fontSize: 11.5, fontWeight: FontWeight.w800, letterSpacing: 1.1),
          ),
          const SizedBox(height: 8),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text(heure, style: Bz.chiffres.copyWith(fontSize: 46, fontWeight: FontWeight.w800, letterSpacing: -1.5, height: 1)),
              if (plan.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(left: 10, bottom: 6),
                  child: Text(plan, style: Bz.chiffres.copyWith(color: Bz.encre3, fontSize: 16, decoration: TextDecoration.lineThrough)),
                ),
              const Spacer(),
              if (statut != null) PastilleStatut(statut: statut, texte: etiquette),
            ],
          ),
          const SizedBox(height: 10),
          Text(route, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700, letterSpacing: -0.2)),
          const SizedBox(height: 6),
          sousRiche ?? Text(sousTexte, style: const TextStyle(color: Bz.encre2, fontSize: 14.5, height: 1.4)),
          const SizedBox(height: 16),
          Wrap(
            spacing: 10,
            runSpacing: 10,
            children: [
              BoutonBz(
                label: pdfOk ? 'Attestation ${s(pdf['mois_libelle'])}' : 'Attestation (après un trajet validé)',
                icone: Icons.description_outlined,
                onPressed: pdfOk ? () => pdfAvecToast(context, widget.api, s(pdf['mois'])) : null,
              ),
              BoutonBz(
                label: 'Déclarer mon trajet',
                variante: VarianteBouton.verre,
                onPressed: () => widget.onAller(1),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _listeTrains(String titre, List<Json> trains, {bool avecTag = false}) {
    return GlassCard(
      padding: const EdgeInsets.fromLTRB(18, 16, 18, 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          TitreSection(titre),
          for (var i = 0; i < trains.length; i++) ...[
            if (i > 0) const SeparateurBz(),
            _ligneTrain(trains[i], avecTag ? (s(trains[i]['relation']) == 'precedent' ? 'avant' : 'après') : null),
          ],
        ],
      ),
    );
  }

  Widget _ligneTrain(Json v, String? tag) {
    final statut = s(v['statut'], 'fluide');
    final supprime = statut == 'supprime';
    final arr = supprime
        ? 'Ne circule pas sur ton trajet'
        : 'Arrivée ${s(v['arrivee_reelle'])}${n(v['retard_arrivee']) >= 1 ? ' (prévue ${s(v['arrivee_plan'])})' : ''}';
    return LigneBz(
      heure: s(v['depart_plan']),
      titre: 'Train ${s(v['numero'])}',
      etiquette: tag == null ? null : Etiquette(tag),
      sous: arr,
      droite: s(v['etiquette']),
      statutDroite: statut,
      barre: supprime,
    );
  }

  List<Widget> _suivants(Json? nt) {
    if (nt == null) return const [];
    final deja = lm(nt['alternatives']).map((x) => s(x['numero'])).toSet();
    final suivants = lm(nt['suivants']).where((v) => !deja.contains(s(v['numero']))).toList();
    if (suivants.isEmpty) return const [];
    return [const SizedBox(height: 14), _listeTrains('Ensuite sur ton trajet', suivants)];
  }

  List<Widget> _carteGreve() {
    final g = _greve;
    if (g == null) return const [];
    final jours = lm(g['jours']);
    final j = jours.where((x) => x['actif'] == true && s(x['mode']) != 'veille' && n(x['nb_supprimes']) > 0).firstOrNull;
    if (j == null) return const [];
    final segments = lm(j['segments']);
    final miens = segments.fold<int>(0, (a, sg) => a + n(sg['nb_supprimes']));
    final total = segments.fold<int>(0, (a, sg) => a + lm(sg['trains']).length);
    final nbSuppr = n(j['nb_supprimes']);
    final texte = '${total > 0 ? 'Dans tes créneaux : $miens train${miens > 1 ? 's' : ''} supprimé${miens > 1 ? 's' : ''} sur $total. ' : ''}'
        'Sur tout le Sillon : $nbSuppr supprimés sur ${n(j['nb_total'])} suivis. '
        '${s(j['source']) == 'officiel' ? 'Grille officielle importée.' : 'Détection automatique, affinée au fil des annonces.'}';
    return [
      const SizedBox(height: 14),
      GlassCard(
        teinte: Bz.rouge,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TitreSection('Mouvement social · ${dateFr(s(j['date']))}'),
            Text(texte, style: const TextStyle(color: Bz.encre2, fontSize: 14.5, height: 1.4)),
            const SizedBox(height: 12),
            BoutonBz(label: 'Voir ma grille', petit: true, variante: VarianteBouton.verre, onPressed: () => widget.onAller(4)),
          ],
        ),
      ),
    ];
  }

  Widget _stats(Json st) {
    final retard = n(st['retard_min']), suppr = n(st['suppressions']), trajets = n(st['nb_trajets']);
    final ratio = [retard / 120, suppr / 3].reduce((a, b) => a > b ? a : b);
    final pct = (ratio * 100).round().clamp(0, 100);
    final lib = s(st['statut_gf']).replaceAll(RegExp(r'[*`_]'), '').replaceFirst(RegExp(r'^\S+\s'), '');
    Widget kpi(int v, String etiquette) => Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('$v', style: Bz.chiffres.copyWith(fontSize: 30, fontWeight: FontWeight.w800, letterSpacing: -1)),
              Text(etiquette, style: const TextStyle(color: Bz.encre3, fontSize: 12.5)),
            ],
          ),
        );
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          TitreSection('${s(st['mois_libelle'])} · Garantie Fiabilité'),
          Row(
            children: [
              kpi(retard, 'min de retard'),
              kpi(suppr, 'suppression${suppr > 1 ? 's' : ''}'),
              kpi(trajets, 'trajet${trajets > 1 ? 's' : ''} validé${trajets > 1 ? 's' : ''}'),
            ],
          ),
          const SizedBox(height: 14),
          ClipRRect(
            borderRadius: BorderRadius.circular(99),
            child: Stack(
              children: [
                Container(height: 8, color: Colors.white.withValues(alpha: 0.10)),
                FractionallySizedBox(
                  widthFactor: pct / 100,
                  child: Container(
                    height: 8,
                    decoration: const BoxDecoration(
                      gradient: LinearGradient(colors: [Bz.ok, Bz.ambre]),
                    ),
                  ),
                ),
              ],
            ),
          ),
          Mention('$lib · ${s(st['conseil_gf'])}'),
        ],
      ),
    );
  }
}
