import 'package:flutter/material.dart';

import 'bazo_api.dart';
import 'fmt.dart';
import 'glass.dart';
import 'theme.dart';

/// Port de `chargerTrajet()` : trajets à confirmer, liste des trains du créneau,
/// déclaration (check-in) et propositions du radar.
class TrajetScreen extends StatefulWidget {
  const TrajetScreen({
    super.key,
    required this.api,
    required this.actif,
    required this.onPending,
    required this.onCheckin,
  });

  final BazoApi api;
  final bool actif;
  final ValueChanged<bool> onPending;
  final VoidCallback onCheckin;

  @override
  State<TrajetScreen> createState() => _TrajetScreenState();
}

class _TrajetScreenState extends State<TrajetScreen> {
  String _sens = 'aller';
  Json? _pending;
  Json? _trains;
  String? _erreurTrains;
  bool _chargeTrains = true;
  List<Json> _props = [];
  final _occupes = <String>{};

  @override
  void initState() {
    super.initState();
    _tout();
  }

  @override
  void didUpdateWidget(TrajetScreen old) {
    super.didUpdateWidget(old);
    if (widget.actif && !old.actif) _tout();
  }

  Future<void> _tout() async {
    await Future.wait([_chargerPending(), _chargerTrains(), _chargerProps()]);
  }

  Future<void> _chargerPending() async {
    try {
      final p = await widget.api.pending();
      if (!mounted) return;
      setState(() => _pending = p);
      widget.onPending(n(p['nb_pending']) > 0);
    } catch (_) {
      if (mounted) setState(() => _pending = null);
    }
  }

  Future<void> _chargerTrains() async {
    setState(() {
      _chargeTrains = true;
      _erreurTrains = null;
    });
    try {
      final d = await widget.api.trainsDuSens(_sens);
      if (mounted) setState(() => _trains = d);
    } on ApiException catch (e) {
      if (mounted) setState(() => _erreurTrains = e.message);
    } catch (_) {
      if (mounted) setState(() => _erreurTrains = 'Impossible de charger les trains.');
    } finally {
      if (mounted) setState(() => _chargeTrains = false);
    }
  }

  Future<void> _chargerProps() async {
    try {
      final p = await widget.api.propositions();
      if (mounted) setState(() => _props = lm(p['propositions']).take(8).toList());
    } catch (_) {
      if (mounted) setState(() => _props = []);
    }
  }

  Future<void> _checkout(int id, String action) async {
    final cle = 'co$id$action';
    setState(() => _occupes.add(cle));
    try {
      final r = await widget.api.checkoutAction(id, action);
      if (!mounted) return;
      final st = s(r['statut']);
      toast(
        context,
        st == 'annule' ? 'Trajet retiré.' : (st == 'valide' ? 'Trajet validé — merci !' : 'Trajet enregistré ($st).'),
        ok: true,
      );
      await _chargerPending();
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message, erreur: true);
    } finally {
      if (mounted) setState(() => _occupes.remove(cle));
    }
  }

  String _destinationCourte(Object? dest, Object? arrivee) {
    final d = s(dest).replaceAll(RegExp(r'\s*\(.*\)\s*$'), '').trim();
    return d.isNotEmpty && d.toLowerCase() != s(arrivee).toLowerCase() ? d : '';
  }

  Future<void> _declarer(Json d, Json t) async {
    final supprime = t['supprime'] == true;
    final ok = await confirmer(
      context,
      titre: 'Train de ${s(t['heure_theo'])}',
      texte: '${s(d['gare_depart'])} → ${s(d['gare_arrivee'])} · train ${s(t['numero_train'])}.'
          '${supprime ? '\n\nCe train est annoncé supprimé. Le déclarer quand même permet de compter la suppression sur ton attestation.' : ''}'
          '\n\nAprès le trajet, Bazo te demandera comment ça s\'est passé.',
      ok: 'Je prends ce train',
    );
    if (!ok || !mounted) return;
    try {
      await widget.api.checkin(
        sens: _sens,
        numeroTrain: s(t['numero_train']),
        heureTheo: s(t['heure_theo']),
        heureArriveeTheo: t['heure_arrivee_theo'] as String?,
        destination: t['destination'] as String?,
        date: s(d['date']),
      );
      if (!mounted) return;
      toast(context, 'Trajet déclaré. Bon voyage !', ok: true);
      widget.onCheckin();
      _chargerPending();
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message, erreur: true);
    } catch (_) {
      if (mounted) toast(context, 'Impossible de déclarer ce trajet.', erreur: true);
    }
  }

  Future<void> _proposition(Json p, String action) async {
    final cle = 'p${p['id']}$action';
    setState(() => _occupes.add(cle));
    try {
      await widget.api.repondreProposition(n(p['id']), action);
      if (!mounted) return;
      toast(context, action == 'accepter' ? 'Incident ajouté à ton attestation.' : 'Proposition écartée.', ok: true);
      await _chargerProps();
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message, erreur: true);
    } finally {
      if (mounted) setState(() => _occupes.remove(cle));
    }
  }

  @override
  Widget build(BuildContext context) {
    final bas = MediaQuery.of(context).padding.bottom + 108;
    final haut = MediaQuery.of(context).padding.top + 12;
    return RefreshIndicator(
      color: Bz.indigo,
      backgroundColor: const Color(0xFF1A2440),
      onRefresh: _tout,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(parent: BouncingScrollPhysics()),
        padding: EdgeInsets.fromLTRB(16, haut, 16, bas),
        children: [
          const EntetePage(
            titre: 'Mon trajet',
            sous: 'Déclare le train que tu prends : chaque minute de retard validée compte pour ton attestation.',
          ),
          ..._blocPending(),
          _selecteurSens(),
          const SizedBox(height: 12),
          _blocTrains(),
          ..._blocProps(),
        ],
      ),
    );
  }

  List<Widget> _blocPending() {
    final p = _pending;
    if (p == null || n(p['nb_pending']) == 0) return const [];
    final trajets = lm(p['trajets']);
    return [
      GlassCard(
        fort: true,
        teinte: Bz.indigo,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TitreSection('À confirmer · ${dateFr(s(p['date']))}'),
            for (var i = 0; i < trajets.length; i++) ...[
              if (i > 0) const Padding(padding: EdgeInsets.symmetric(vertical: 8), child: SeparateurBz()),
              Text(
                '${s(trajets[i]['heure_theo'])} · Train ${s(trajets[i]['numero_train'])}',
                style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 16),
              ),
              Text(
                '${s(trajets[i]['gare_depart'])} → ${s(trajets[i]['gare_arrivee'])}',
                style: const TextStyle(color: Bz.encre2, fontSize: 13.5),
              ),
              const SizedBox(height: 6),
              const Text('Comment s\'est passé ce trajet ?', style: TextStyle(color: Bz.encre3, fontSize: 13.5)),
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final a in const [
                    ('ok', 'À l\'heure', VarianteBouton.ok),
                    ('suiv', 'Pris le suivant', VarianteBouton.ambre),
                    ('suppr', 'Supprimé', VarianteBouton.danger),
                    ('bus', 'Bus de substitution', VarianteBouton.ambre),
                    ('rien', 'Pas pris', VarianteBouton.verre),
                  ])
                    BoutonBz(
                      label: a.$2,
                      petit: true,
                      variante: a.$3,
                      onPressed: _occupes.contains('co${trajets[i]['id']}${a.$1}')
                          ? null
                          : () => _checkout(n(trajets[i]['id']), a.$1),
                    ),
                ],
              ),
            ],
          ],
        ),
      ),
      const SizedBox(height: 14),
    ];
  }

  Widget _selecteurSens() {
    Widget onglet(String sens, String label) {
      final actif = _sens == sens;
      return Expanded(
        child: GestureDetector(
          onTap: () {
            if (_sens == sens) return;
            setState(() => _sens = sens);
            _chargerTrains();
          },
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 220),
            padding: const EdgeInsets.symmetric(vertical: 11),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(14),
              color: actif ? Colors.white.withValues(alpha: 0.16) : Colors.transparent,
              border: Border.all(color: actif ? Bz.bordVerre : Colors.transparent),
            ),
            child: Center(
              child: Text(
                label,
                style: TextStyle(fontWeight: FontWeight.w700, color: actif ? Bz.encre : Bz.encre2),
              ),
            ),
          ),
        ),
      );
    }

    return GlassCard(
      rayon: 18,
      padding: const EdgeInsets.all(5),
      child: Row(children: [onglet('aller', 'Aller'), onglet('retour', 'Retour')]),
    );
  }

  Widget _blocTrains() {
    final d = _trains;
    if (_chargeTrains && d == null) return const Squelette(lignes: 4);
    if (_erreurTrains != null && d == null) {
      return GlassCard(child: VideBz(titre: 'Impossible de charger les trains', texte: _erreurTrains));
    }
    if (d == null) return const SizedBox.shrink();
    final trains = lm(d['trains']);
    final ctx = '${s(d['gare_depart'])} → ${s(d['gare_arrivee'])} · ${s(d['tranche'])} · ${dateFr(s(d['date']))}';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(4, 0, 4, 10),
          child: Text(ctx, style: const TextStyle(color: Bz.encre2, fontSize: 13.5)),
        ),
        GlassCard(
          padding: const EdgeInsets.fromLTRB(18, 16, 18, 6),
          child: trains.isEmpty
              ? const VideBz(
                  titre: 'Aucun train trouvé',
                  texte: 'Vérifie ton créneau dans Moi › Mon profil, ou réessaie dans un instant.',
                )
              : Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const TitreSection('Quel train prends-tu ?'),
                    for (var i = 0; i < trains.length; i++) ...[
                      if (i > 0) const SeparateurBz(),
                      _ligne(d, trains[i]),
                    ],
                  ],
                ),
        ),
      ],
    );
  }

  Widget _ligne(Json d, Json t) {
    final supprime = t['supprime'] == true;
    final dest = _destinationCourte(t['destination'], d['gare_arrivee']);
    final arr = s(t['heure_arrivee_theo']);
    final sous = [
      if (arr.isNotEmpty) 'arrivée $arr',
      if (dest.isNotEmpty) 'direction $dest',
    ].join(' · ');
    return LigneBz(
      heure: s(t['heure_theo']),
      titre: 'Train ${s(t['numero_train'])}',
      sous: sous,
      droite: supprime ? 'Supprimé' : 'Choisir',
      statutDroite: supprime ? 'supprime' : null,
      barre: supprime,
      onTap: () => _declarer(d, t),
    );
  }

  List<Widget> _blocProps() {
    if (_props.isEmpty) return const [];
    return [
      const SizedBox(height: 14),
      GlassCard(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const TitreSection('Le radar a vu un incident sur tes trains'),
            const Text(
              'Tu étais à bord ? Confirme pour l\'ajouter à ton attestation. Sinon, écarte-le.',
              style: TextStyle(color: Bz.encre2, fontSize: 13.5, height: 1.35),
            ),
            const SizedBox(height: 8),
            for (var i = 0; i < _props.length; i++) ...[
              if (i > 0) const SeparateurBz(),
              _proposition_(_props[i]),
            ],
          ],
        ),
      ),
    ];
  }

  Widget _proposition_(Json x) {
    final incident = s(x['type_incident']) == 'supprime' ? 'supprimé' : '+${n(x['retard_min'])} min';
    final motif = s(x['motif']);
    return LigneBz(
      titre: '${jourMois(s(x['date']))} · ${s(x['heure_theo'])} · Train ${s(x['numero_train'])}',
      sous: '${s(x['gare_depart'])} → ${s(x['gare_arrivee'])} · $incident${motif.isNotEmpty ? ' · $motif' : ''}',
      enfantBas: Row(
        children: [
          BoutonBz(
            label: 'J\'y étais',
            petit: true,
            variante: VarianteBouton.ok,
            onPressed: _occupes.contains('p${x['id']}accepter') ? null : () => _proposition(x, 'accepter'),
          ),
          const SizedBox(width: 8),
          BoutonBz(
            label: 'Non',
            petit: true,
            variante: VarianteBouton.verre,
            onPressed: _occupes.contains('p${x['id']}refuser') ? null : () => _proposition(x, 'refuser'),
          ),
        ],
      ),
    );
  }
}
