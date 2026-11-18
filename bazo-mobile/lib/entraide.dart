import 'package:flutter/material.dart';

import 'bazo_api.dart';
import 'fmt.dart';
import 'glass.dart';
import 'theme.dart';

/// Port de `chargerHub()` / `carteHub()` / `ouvrirOffre()` : entraide entre voyageurs
/// quand un train ne vient pas. Partage de frais uniquement, rendez-vous en gare.
class EntraideScreen extends StatefulWidget {
  const EntraideScreen({super.key, required this.api, required this.actif});

  final BazoApi api;
  final bool actif;

  @override
  State<EntraideScreen> createState() => _EntraideScreenState();
}

class _EntraideScreenState extends State<EntraideScreen> {
  Json? _hub;
  String? _erreur;
  bool _charge = true;

  @override
  void initState() {
    super.initState();
    _charger();
  }

  @override
  void didUpdateWidget(EntraideScreen old) {
    super.didUpdateWidget(old);
    if (widget.actif && !old.actif) _charger();
  }

  Future<void> _charger() async {
    try {
      final h = await widget.api.hub();
      if (mounted) {
        setState(() {
        _hub = h;
        _erreur = null;
        });
      }
    } on ApiException catch (e) {
      if (mounted) setState(() => _erreur = e.message);
    } catch (_) {
      if (mounted) setState(() => _erreur = 'Impossible de charger l\'entraide.');
    } finally {
      if (mounted) setState(() => _charge = false);
    }
  }

  Future<void> _agir(Future<void> Function() action, String succes) async {
    try {
      await action();
      if (!mounted) return;
      toast(context, succes, ok: true);
      await _charger();
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message, erreur: true);
    } catch (_) {
      if (mounted) toast(context, 'Action impossible pour le moment.', erreur: true);
    }
  }

  Future<void> _bloque(Json h, Json m) async {
    final ok = await confirmer(
      context,
      titre: 'Te déclarer bloqué ?',
      texte: 'Train ${s(m['numero_train'])} de ${s(m['heure_theo'])} à ${s(m['gare_depart'])}. '
          'Les autres voyageurs de ce train le verront. Signalement communautaire, sans effet sur ton attestation.',
      ok: 'Je suis bloqué',
      variante: VarianteBouton.ambre,
    );
    if (!ok) return;
    await _agir(
      () => widget.api.jeSuisBloque(
        date: s(h['date']),
        numeroTrain: s(m['numero_train']),
        heureTheo: s(m['heure_theo']),
        gare: s(m['gare_depart']),
      ),
      'C\'est noté — courage.',
    );
  }

  Future<void> _reserver(Json o) async {
    try {
      final r = await widget.api.reserverPlace(n(o['id']));
      if (!mounted) return;
      final contact = s(m(r['offre'])['contact']);
      toast(context, 'Place réservée · contact : ${contact.isEmpty ? 'via le conducteur' : contact}', ok: true);
      await _charger();
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message, erreur: true);
    }
  }

  Future<void> _abus(Json o) async {
    final ok = await confirmer(
      context,
      titre: 'Signaler cette offre ?',
      texte: 'Pour un comportement inapproprié, un prix abusif ou une offre fantôme. Trois signalements suspendent l\'offre.',
      ok: 'Signaler',
      variante: VarianteBouton.danger,
    );
    if (!ok) return;
    await _agir(() => widget.api.signalerOffre(n(o['id'])), 'Merci, l\'offre est surveillée.');
  }

  @override
  Widget build(BuildContext context) {
    final bas = MediaQuery.of(context).padding.bottom + 108;
    final haut = MediaQuery.of(context).padding.top + 12;
    return RefreshIndicator(
      color: Bz.indigo,
      backgroundColor: const Color(0xFF1A2440),
      onRefresh: _charger,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(parent: BouncingScrollPhysics()),
        padding: EdgeInsets.fromLTRB(16, haut, 16, bas),
        children: [
          const EntetePage(
            titre: 'Entraide',
            sous: 'Quand un train ne vient pas, les voyageurs du même quai s\'organisent. Partage de frais uniquement, jamais une annonce SNCF.',
          ),
          ..._corps(),
        ],
      ),
    );
  }

  List<Widget> _corps() {
    if (_charge && _hub == null) return const [Squelette(lignes: 4)];
    final h = _hub;
    if (h == null) return [GlassCard(child: VideBz(titre: 'Impossible de charger l\'entraide', texte: _erreur))];
    final hubs = lm(h['hubs']);
    final mes = lm(h['mes_checkins']);
    final seuils = m(h['seuils']);
    final quorum = n(seuils['quorum']);
    return [
      if (hubs.isEmpty)
        GlassCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const TitreSection('Comment ça marche'),
              Text(
                'Un point d\'entraide s\'ouvre automatiquement quand un train est supprimé, en retard de ${n(seuils['retard_min'])} min ou plus, '
                'ou quand $quorum voyageurs se déclarent bloqués. Si l\'attente dépasse ${n(seuils['attente_covoit_min']) ~/ 60} h, '
                'les conducteurs peuvent proposer des places — partage de frais plafonné (${s(h['bareme_eur_km'])} €/km partagé), rendez-vous en gare uniquement.',
                style: const TextStyle(color: Bz.encre2, fontSize: 14.5, height: 1.4),
              ),
            ],
          ),
        ),
      for (final x in hubs) ...[
        const SizedBox(height: 14),
        _carteHub(x, h),
      ],
      const SizedBox(height: 14),
      GlassCard(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const TitreSection('Je suis bloqué'),
            if (mes.isEmpty)
              const VideBz(
                titre: 'Déclare d\'abord ton trajet',
                texte: 'Le bouton « Je suis bloqué » s\'active pour les trains que tu as déclarés aujourd\'hui.',
              )
            else ...[
              Text(
                'Ton train ne part pas ou s\'arrête ? Préviens les voyageurs du même quai. À $quorum personnes, un point d\'entraide s\'ouvre.',
                style: const TextStyle(color: Bz.encre2, fontSize: 13.5, height: 1.35),
              ),
              const SizedBox(height: 12),
              for (final x in mes)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: BoutonBz(
                    label: '${s(x['heure_theo'])} · Train ${s(x['numero_train'])} depuis ${s(x['gare_depart'])}',
                    variante: VarianteBouton.ambre,
                    pleineLargeur: true,
                    onPressed: () => _bloque(h, x),
                  ),
                ),
            ],
          ],
        ),
      ),
      Padding(padding: const EdgeInsets.symmetric(horizontal: 4), child: Mention(s(h['mention']))),
    ];
  }

  Widget _carteHub(Json x, Json h) {
    final actif = s(x['statut']) == 'actif';
    final ferme = s(x['statut']) == 'ferme';
    final seuils = m(h['seuils']);
    final nbBloques = n(x['nb_bloques']);
    final motifs = {
      'train_supprime': 'train supprimé',
      'terminus_anticipe': 'terminus anticipé',
      'retard_60': 'retard ≥ 60 min',
      'quorum_3_bloques': '$nbBloques voyageurs bloqués',
    };
    var motif = motifs[s(x['motif'])];
    if (motif == null && s(x['motif']).startsWith('quorum')) motif = '$nbBloques voyageurs bloqués';
    final attente = x['attente_min'] == null ? null : n(x['attente_min']);
    final offres = lm(x['offres']);

    String duree(int min) =>
        min >= 60 ? '${min ~/ 60} h ${(min % 60).toString().padLeft(2, '0')}' : '$min min';

    return GlassCard(
      teinte: actif ? Bz.rouge : null,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  '${s(x['gare'])} · Train ${s(x['numero_train'])} de ${s(x['heure_theo'])}',
                  style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w800, letterSpacing: -0.2),
                ),
              ),
              const SizedBox(width: 8),
              PastilleStatut(
                statut: actif ? 'supprime' : (ferme ? null : 'retard'),
                texte: actif ? 'Actif' : (ferme ? 'Fermé' : 'En attente'),
                petite: true,
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            (actif
                    ? 'Point d\'entraide ouvert${motif != null ? ' — $motif' : ''}.'
                    : '$nbBloques/${n(seuils['quorum'])} voyageurs bloqués${x['moi_bloque'] == true ? ' (dont toi)' : ''}. L\'entraide s\'ouvre au seuil ou si le radar confirme l\'incident.') +
                (attente != null ? ' Prochain train utile dans ${duree(attente)}.' : ''),
            style: const TextStyle(color: Bz.encre2, fontSize: 14.5, height: 1.4),
          ),
          if (actif && x['covoiturage_ouvert'] == true) ...[
            const SizedBox(height: 10),
            const Text(
              'Entraide voiture ouverte — attente de plus de 2 h. Rendez-vous en gare uniquement.',
              style: TextStyle(fontWeight: FontWeight.w700, fontSize: 14),
            ),
            const SizedBox(height: 6),
            if (offres.isEmpty)
              const Text('Aucune offre pour l\'instant.', style: TextStyle(color: Bz.encre3))
            else
              for (var i = 0; i < offres.length; i++) ...[
                if (i > 0) const SeparateurBz(),
                _offre(offres[i]),
              ],
            const SizedBox(height: 12),
            BoutonBz(
              label: 'Je conduis, je propose des places',
              icone: Icons.directions_car_rounded,
              pleineLargeur: true,
              onPressed: () => _ouvrirOffre(x, h),
            ),
          ] else if (actif) ...[
            const SizedBox(height: 8),
            const Text(
              'L\'entraide voiture s\'ouvre si l\'attente dépasse 2 h. En attendant : alternatives sur l\'écran Accueil.',
              style: TextStyle(color: Bz.encre3, fontSize: 13.5, height: 1.35),
            ),
          ],
        ],
      ),
    );
  }

  Widget _offre(Json o) {
    final mine = o['est_conducteur'] == true;
    final passager = o['est_passager'] == true;
    final statut = s(o['statut']);
    final places = n(o['places_restantes']);
    final participation = (o['participation_eur'] as num?) ?? 0;
    final contact = s(o['contact']);
    final passagers = (o['passagers'] as List?)?.map((e) => '$e').toList() ?? const <String>[];

    final actions = <Widget>[];
    if (mine) {
      if (statut == 'ouverte' || statut == 'complete') {
        actions.add(BoutonBz(
          label: 'Je pars',
          petit: true,
          variante: VarianteBouton.ok,
          onPressed: () => _agir(() => widget.api.statutOffre(n(o['id']), 'partie'), 'Bonne route !'),
        ));
        actions.add(BoutonBz(
          label: 'Annuler',
          petit: true,
          variante: VarianteBouton.verre,
          onPressed: () => _agir(() => widget.api.statutOffre(n(o['id']), 'annulee'), 'Offre annulée.'),
        ));
      }
    } else if (passager) {
      actions.add(BoutonBz(
        label: 'Annuler ma place',
        petit: true,
        variante: VarianteBouton.verre,
        onPressed: () => _agir(() => widget.api.annulerPlace(n(o['id'])), 'Réservation annulée.'),
      ));
    } else if (statut == 'ouverte' && places > 0) {
      actions.add(BoutonBz(label: 'Réserver', petit: true, onPressed: () => _reserver(o)));
      actions.add(BoutonBz(label: '⚑', petit: true, variante: VarianteBouton.verre, onPressed: () => _abus(o)));
    }

    final libelles = {
      'ouverte': '$places place${places > 1 ? 's' : ''}',
      'complete': 'Complet',
      'partie': 'Partie',
      'annulee': 'Annulée',
      'suspendue': 'Suspendue',
    };
    return LigneBz(
      titre: '${s(o['conducteur'])} → ${s(o['destination'])} · départ ${s(o['depart'])}',
      sous: '${s(o['point_rdv'])} · ${participation > 0 ? '${euros(participation)} € par passager' : 'gratuit'}'
          '${contact.isNotEmpty ? ' · $contact' : ''}'
          '${mine && passagers.isNotEmpty ? ' · passagers : ${passagers.join(', ')}' : ''}',
      droite: libelles[statut] ?? statut,
      statutDroite: statut == 'ouverte' ? 'fluide' : null,
      enfantBas: actions.isEmpty ? null : Wrap(spacing: 8, runSpacing: 8, children: actions),
    );
  }

  Future<void> _ouvrirOffre(Json hub, Json h) async {
    List<String> gares;
    try {
      gares = await widget.api.gares();
    } catch (_) {
      gares = const ['Metz', 'Thionville', 'Luxembourg', 'Nancy'];
    }
    gares = gares.where((g) => g != s(hub['gare'])).toList();
    if (gares.isEmpty || !mounted) return;
    final points = m(h['points_rdv']);
    if (points.isEmpty) {
      toast(context, 'Aucun point de rendez-vous disponible.', erreur: true);
      return;
    }
    final dans15 = DateTime.now().add(const Duration(minutes: 15));
    final hh = '${dans15.hour.toString().padLeft(2, '0')}h${dans15.minute.toString().padLeft(2, '0')}';

    final publie = await feuille<bool>(
      context,
      (ctx) => _FormOffre(
        api: widget.api,
        hub: hub,
        h: h,
        gares: gares,
        points: points,
        departInitial: hh,
      ),
    );
    if (publie == true && mounted) await _charger();
  }
}

class _FormOffre extends StatefulWidget {
  const _FormOffre({
    required this.api,
    required this.hub,
    required this.h,
    required this.gares,
    required this.points,
    required this.departInitial,
  });

  final BazoApi api;
  final Json hub;
  final Json h;
  final List<String> gares;
  final Json points;
  final String departInitial;

  @override
  State<_FormOffre> createState() => _FormOffreState();
}

class _FormOffreState extends State<_FormOffre> {
  late String _dest = widget.gares.first;
  late String _rdv = widget.points.keys.first;
  int _places = 3;
  late final _depart = TextEditingController(text: widget.departInitial);
  final _eur = TextEditingController(text: '0');
  final _contact = TextEditingController();
  bool _charte = false;
  bool _envoi = false;
  String? _erreur;

  @override
  void dispose() {
    _depart.dispose();
    _eur.dispose();
    _contact.dispose();
    super.dispose();
  }

  Future<void> _publier() async {
    if (!RegExp(r'^\d{2}h\d{2}$').hasMatch(_depart.text.trim())) {
      setState(() => _erreur = 'Heure de départ au format 08h30.');
      return;
    }
    if (!_charte) {
      setState(() => _erreur = 'Accepte la charte pour publier.');
      return;
    }
    setState(() {
      _envoi = true;
      _erreur = null;
    });
    try {
      final r = await widget.api.publierOffre({
        'hub_id': widget.hub['id'],
        'destination': _dest,
        'places': _places,
        'point_rdv_code': _rdv,
        'depart': _depart.text.trim(),
        'participation_eur': double.tryParse(_eur.text.replaceAll(',', '.')) ?? 0,
        'contact': _contact.text.trim().isEmpty ? null : _contact.text.trim(),
        'accepte_charte': _charte,
      });
      if (!mounted) return;
      final plafond = r['plafond_eur'] as num?;
      Navigator.of(context).pop(true);
      toast(context, 'Offre publiée${plafond != null ? ' · plafond ${euros(plafond)} € par passager' : ''}', ok: true);
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
        _erreur = e.message;
        _envoi = false;
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
        _erreur = 'Publication impossible.';
        _envoi = false;
        });
      }
    }
  }

  Widget _liste<T>({required String label, required T valeur, required List<DropdownMenuItem<T>> items, required ValueChanged<T?> onChanged}) {
    return DropdownButtonFormField<T>(
      initialValue: valeur,
      items: items,
      onChanged: onChanged,
      dropdownColor: const Color(0xFF1A2440),
      decoration: InputDecoration(labelText: label),
      isExpanded: true,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('Proposer des places', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: -0.4)),
        const SizedBox(height: 6),
        Text(
          'Depuis ${s(widget.hub['gare'])}. Dépose en gare uniquement. Participation plafonnée à ${s(widget.h['bareme_eur_km'])} €/km partagés entre tous.',
          style: const TextStyle(color: Bz.encre2, height: 1.35),
        ),
        const SizedBox(height: 16),
        _liste<String>(
          label: 'Destination',
          valeur: _dest,
          items: [for (final g in widget.gares) DropdownMenuItem(value: g, child: Text(g))],
          onChanged: (v) => setState(() => _dest = v ?? _dest),
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(
              child: _liste<int>(
                label: 'Places',
                valeur: _places,
                items: [for (final p in const [1, 2, 3, 4]) DropdownMenuItem(value: p, child: Text('$p'))],
                onChanged: (v) => setState(() => _places = v ?? _places),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: TextField(controller: _depart, decoration: const InputDecoration(labelText: 'Départ (08h30)')),
            ),
          ],
        ),
        const SizedBox(height: 12),
        _liste<String>(
          label: 'Point de rendez-vous',
          valeur: _rdv,
          items: [for (final e in widget.points.entries) DropdownMenuItem(value: e.key, child: Text('${e.value}', overflow: TextOverflow.ellipsis))],
          onChanged: (v) => setState(() => _rdv = v ?? _rdv),
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(
              child: TextField(
                controller: _eur,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                decoration: const InputDecoration(labelText: 'Participation (€)'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: TextField(
                controller: _contact,
                maxLength: 80,
                decoration: const InputDecoration(labelText: 'Contact visible', counterText: ''),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        InkWell(
          onTap: () => setState(() => _charte = !_charte),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Checkbox(value: _charte, onChanged: (v) => setState(() => _charte = v ?? false), activeColor: Bz.indigo),
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.only(top: 12),
                  child: Text(s(widget.h['charte']), style: const TextStyle(color: Bz.encre2, fontSize: 13, height: 1.35)),
                ),
              ),
            ],
          ),
        ),
        if (_erreur != null)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(_erreur!, style: const TextStyle(color: Bz.rouge, fontWeight: FontWeight.w600)),
          ),
        const SizedBox(height: 14),
        BoutonBz(label: _envoi ? 'Publication…' : 'Publier mon offre', pleineLargeur: true, onPressed: _envoi ? null : _publier),
      ],
    );
  }
}
