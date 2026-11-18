import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';

import 'actions.dart';
import 'bazo_api.dart';
import 'fmt.dart';
import 'glass.dart';
import 'theme.dart';

const _tranches = ['04h-06h', '06h-08h', '08h-10h', '10h-12h', '12h-14h', '14h-16h', '16h-18h', '18h-20h', '20h-22h'];
const _portailSncf = 'https://www.sncf-voyageurs.com/fr/contactez-nous/demande-et-reclamation/';

/// Port de `chargerMoi()` : profil, alertes, habituels, grève, attestation, compte.
class MoiScreen extends StatefulWidget {
  const MoiScreen({super.key, required this.api, required this.actif, required this.onDeconnexion});

  final BazoApi api;
  final bool actif;

  /// Appelé après déconnexion ou suppression (le message est affiché sur l'écran de connexion).
  final void Function(String message) onDeconnexion;

  @override
  State<MoiScreen> createState() => _MoiScreenState();
}

class _MoiScreenState extends State<MoiScreen> {
  Json? _profil;
  List<Json> _habituels = [];
  Json? _greve;
  List<Json> _mois = [];
  Json? _statut;
  String? _erreur;
  bool _charge = true;
  String? _moisChoisi;
  bool _pdfEnCours = false;
  String _sens = 'aller';
  final _heureHab = TextEditingController();

  @override
  void initState() {
    super.initState();
    _charger();
  }

  @override
  void dispose() {
    _heureHab.dispose();
    super.dispose();
  }

  @override
  void didUpdateWidget(MoiScreen old) {
    super.didUpdateWidget(old);
    if (widget.actif && !old.actif) _charger();
  }

  Future<T?> _ou<T>(Future<T> f) async {
    try {
      return await f;
    } catch (_) {
      return null;
    }
  }

  Future<void> _charger() async {
    try {
      final r = await Future.wait<Object?>([
        widget.api.profilComplet(),
        _ou(widget.api.habituels()),
        _ou(widget.api.greve()),
        _ou(widget.api.pdfMoisDispo()),
        _ou(widget.api.statutMois()),
      ]);
      if (!mounted) return;
      final mois = lm(m(r[3])['mois']);
      setState(() {
        _profil = r[0] as Json;
        _habituels = lm(m(r[1])['habituels']);
        _greve = r[2] as Json?;
        _mois = mois;
        _statut = r[4] as Json?;
        _moisChoisi = mois.any((x) => s(x['code']) == _moisChoisi) ? _moisChoisi : (mois.isEmpty ? null : s(mois.first['code']));
        _erreur = null;
      });
    } on ApiException catch (e) {
      if (mounted) setState(() => _erreur = e.message);
    } catch (_) {
      if (mounted) setState(() => _erreur = 'Impossible de charger ton profil.');
    } finally {
      if (mounted) setState(() => _charge = false);
    }
  }

  void _erreurToast(Object e) {
    if (!mounted) return;
    toast(context, e is ApiException ? e.message : 'Action impossible pour le moment.', erreur: true);
  }

  Future<void> _alertes(bool v) async {
    final avant = _profil;
    setState(() => _profil = {...?_profil, 'alertes_actives': v});
    try {
      await widget.api.majProfil({'alertes_actives': v});
      if (mounted) toast(context, v ? 'Alertes activées.' : 'Alertes en pause.', ok: true);
    } catch (e) {
      if (mounted) setState(() => _profil = avant);
      _erreurToast(e);
    }
  }

  Future<void> _ajouterHabituel() async {
    final h = _heureHab.text.trim();
    if (!RegExp(r'^\d{2}h\d{2}$').hasMatch(h)) {
      toast(context, 'Heure au format 08h33.', erreur: true);
      return;
    }
    try {
      await widget.api.ajouterHabituel(_sens, h);
      _heureHab.clear();
      if (mounted) toast(context, 'Train habituel ajouté.', ok: true);
      await _charger();
    } catch (e) {
      _erreurToast(e);
    }
  }

  Future<void> _retirerHabituel(Json x) async {
    final p = _profil;
    final sens = s(x['gare_depart']) == s(p?['gare_depart']) ? 'aller' : 'retour';
    try {
      await widget.api.retirerHabituel(sens, s(x['heure']));
      if (mounted) toast(context, 'Retiré.', ok: true);
      await _charger();
    } catch (e) {
      _erreurToast(e);
    }
  }

  Future<void> _pdf() async {
    final mois = _moisChoisi;
    if (mois == null) return;
    setState(() => _pdfEnCours = true);
    await pdfAvecToast(context, widget.api, mois);
    if (mounted) setState(() => _pdfEnCours = false);
  }

  Future<void> _editerProfil() async {
    final p = _profil;
    if (p == null) return;
    List<String> gares;
    try {
      gares = await widget.api.gares();
    } catch (_) {
      gares = [s(p['gare_depart']), s(p['gare_retour'])];
    }
    for (final g in [s(p['gare_depart']), s(p['gare_retour'])]) {
      if (g.isNotEmpty && !gares.contains(g)) gares = [...gares, g];
    }
    if (!mounted) return;
    final ok = await feuille<bool>(context, (ctx) => _FormProfil(api: widget.api, profil: p, gares: gares));
    if (ok == true) {
      if (mounted) toast(context, 'Profil mis à jour.', ok: true);
      await _charger();
    }
  }

  Future<void> _kit() async {
    Json? kit;
    String? erreur;
    Future<void> charger() async {
      try {
        kit = await widget.api.kitReclamation();
      } catch (e) {
        erreur = e is ApiException ? e.message : 'Kit indisponible.';
      }
    }

    final futur = charger();
    await feuille<void>(
      context,
      (ctx) => FutureBuilder<void>(
        future: futur,
        builder: (ctx, snap) {
          if (snap.connectionState != ConnectionState.done) return const Squelette(lignes: 4);
          final k = kit;
          if (k == null) return VideBz(titre: 'Kit réclamation', texte: erreur);
          final brut = s(k['texte']).replaceAll(RegExp(r'[*`]'), '');
          final bloc = RegExp(r'```([\s\S]*?)```').firstMatch(s(k['texte']))?.group(1)?.replaceAll(RegExp(r'[*`]'), '');
          final texte = (bloc ?? brut).trim();
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Kit réclamation · ${s(k['mois'])}', style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: -0.4)),
              const SizedBox(height: 6),
              const Text('Joins ton attestation PDF sur le portail SNCF et colle ce texte.', style: TextStyle(color: Bz.encre2, height: 1.35)),
              const SizedBox(height: 14),
              Row(
                children: [
                  Expanded(
                    child: BoutonBz(
                      label: 'Portail SNCF',
                      icone: Icons.open_in_new_rounded,
                      onPressed: () => launchUrl(Uri.parse(_portailSncf), mode: LaunchMode.externalApplication),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: BoutonBz(
                      label: 'Copier',
                      icone: Icons.copy_rounded,
                      variante: VarianteBouton.verre,
                      onPressed: () async {
                        await Clipboard.setData(ClipboardData(text: texte));
                        if (mounted) toast(context, 'Texte copié.', ok: true);
                      },
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(
                  color: Colors.black.withValues(alpha: 0.25),
                  borderRadius: BorderRadius.circular(14),
                  border: Border.all(color: Bz.bordVerre),
                ),
                child: SelectableText(texte, style: const TextStyle(fontSize: 13.5, height: 1.45, color: Bz.encre)),
              ),
            ],
          );
        },
      ),
    );
  }

  Future<void> _incidents() async {
    Json? d;
    String? erreur;
    Future<void> charger() async {
      try {
        d = await widget.api.incidents();
      } catch (e) {
        erreur = e is ApiException ? e.message : 'Incidents indisponibles.';
      }
    }

    final futur = charger();
    await feuille<void>(
      context,
      (ctx) => FutureBuilder<void>(
        future: futur,
        builder: (ctx, snap) {
          if (snap.connectionState != ConnectionState.done) return const Squelette(lignes: 4);
          final r = d;
          if (r == null) return VideBz(titre: 'Mes incidents', texte: erreur);
          final liste = lm(r['incidents']);
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Mes incidents · ${s(r['mois'])}', style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: -0.4)),
              const SizedBox(height: 10),
              if (liste.isEmpty)
                const VideBz(titre: 'Aucun incident validé ce mois', texte: 'Bonne nouvelle pour tes trajets, moins pour l\'attestation.')
              else
                for (var i = 0; i < liste.length; i++) ...[
                  if (i > 0) const SeparateurBz(),
                  LigneBz(
                    heure: s(liste[i]['heure']),
                    titre: '${jourMois(s(liste[i]['date']))} · Train ${s(liste[i]['train'])}',
                    sous: [
                      s(liste[i]['trajet']),
                      s(liste[i]['motif']),
                      s(liste[i]['provenance_libelle']),
                    ].where((e) => e.isNotEmpty).join(' · '),
                    droite: s(liste[i]['type']) == 'supprime' ? 'Supprimé' : '+${n(liste[i]['retard'])} min',
                    statutDroite: s(liste[i]['type']) == 'supprime' ? 'supprime' : 'retard',
                  ),
                ],
            ],
          );
        },
      ),
    );
  }

  Future<void> _exporter() async {
    try {
      final json = await widget.api.exportDonnees();
      await Clipboard.setData(ClipboardData(text: json));
      if (mounted) toast(context, 'Export copié dans le presse-papiers (${json.length} caractères).', ok: true);
    } catch (e) {
      _erreurToast(e);
    }
  }

  Future<void> _supprimer() async {
    final ok = await confirmer(
      context,
      titre: 'Supprimer ton compte ?',
      texte: 'Profil, trajets et signalements seront effacés définitivement. Les attestations déjà téléchargées restent valables.',
      ok: 'Supprimer définitivement',
      variante: VarianteBouton.danger,
    );
    if (!ok) return;
    try {
      await widget.api.supprimerCompte();
      widget.onDeconnexion('Compte supprimé. À bientôt peut-être.');
    } catch (e) {
      _erreurToast(e);
    }
  }

  @override
  Widget build(BuildContext context) {
    final bas = MediaQuery.of(context).padding.bottom + 108;
    final haut = MediaQuery.of(context).padding.top + 12;
    final p = _profil;
    final prenom = s(p?['prenom']);
    return RefreshIndicator(
      color: Bz.indigo,
      backgroundColor: const Color(0xFF1A2440),
      onRefresh: _charger,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(parent: BouncingScrollPhysics()),
        padding: EdgeInsets.fromLTRB(16, haut, 16, bas),
        children: [
          EntetePage(
            titre: prenom.isNotEmpty && prenom != 'Voyageur' ? 'Bonjour $prenom' : 'Moi',
            sous: p == null ? null : '${s(p['gare_depart'])} ${s(p['heures'])} ⇄ ${s(p['gare_retour'])} ${s(p['heures_retour'])}',
          ),
          if (_charge && p == null)
            const Squelette(lignes: 4)
          else if (p == null)
            GlassCard(child: VideBz(titre: 'Impossible de charger ton profil', texte: _erreur))
          else ...[
            _cartes(p),
          ],
        ],
      ),
    );
  }

  Widget _cartes(Json p) {
    const espace = SizedBox(height: 14);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _carteProfil(p),
        espace,
        _carteHabituels(p),
        espace,
        _carteAttestation(),
        if (_greve != null) ...[espace, _carteGreve(_greve!)],
        espace,
        _carteCompte(),
      ],
    );
  }

  Widget _carteProfil(Json p) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Expanded(child: TitreSection('Mon profil')),
              BoutonBz(label: 'Modifier', petit: true, variante: VarianteBouton.verre, onPressed: _editerProfil),
            ],
          ),
          LigneBz(titre: 'Aller · ${s(p['gare_depart'])} → ${s(p['gare_retour'])}', sous: s(p['heures'])),
          const SeparateurBz(),
          LigneBz(titre: 'Retour · ${s(p['gare_retour'])} → ${s(p['gare_depart'])}', sous: s(p['heures_retour'])),
          const SeparateurBz(),
          Row(
            children: [
              const Expanded(
                child: Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('Alertes temps réel', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 15.5)),
                      SizedBox(height: 2),
                      Text('Retard, suppression, bascule sur un autre train', style: TextStyle(color: Bz.encre2, fontSize: 13)),
                    ],
                  ),
                ),
              ),
              Switch(value: p['alertes_actives'] == true, onChanged: _alertes),
            ],
          ),
        ],
      ),
    );
  }

  Widget _carteHabituels(Json p) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const TitreSection('Mes trains habituels'),
          const Text('Ils sont surveillés en priorité et affichés en premier sur l\'accueil.', style: TextStyle(color: Bz.encre2, fontSize: 13.5, height: 1.35)),
          const SizedBox(height: 8),
          if (_habituels.isEmpty)
            const Padding(padding: EdgeInsets.symmetric(vertical: 8), child: Text('Aucun pour l\'instant.', style: TextStyle(color: Bz.encre3)))
          else
            for (var i = 0; i < _habituels.length; i++) ...[
              if (i > 0) const SeparateurBz(),
              LigneBz(
                heure: s(_habituels[i]['heure']),
                titre: '${s(_habituels[i]['gare_depart'])} → ${s(_habituels[i]['gare_arrivee'])}',
                sous: (_habituels[i]['manuel'] == true ? 'déclaré' : 'déduit de tes trajets') +
                    (s(_habituels[i]['numero_train']).isNotEmpty ? ' · train ${s(_habituels[i]['numero_train'])}' : ''),
                enfantBas: Align(
                  alignment: Alignment.centerLeft,
                  child: BoutonBz(label: 'Retirer', petit: true, variante: VarianteBouton.verre, onPressed: () => _retirerHabituel(_habituels[i])),
                ),
              ),
            ],
          const SizedBox(height: 12),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              SizedBox(
                width: 120,
                child: DropdownButtonFormField<String>(
                  initialValue: _sens,
                  dropdownColor: const Color(0xFF1A2440),
                  decoration: const InputDecoration(labelText: 'Sens'),
                  items: const [
                    DropdownMenuItem(value: 'aller', child: Text('Aller')),
                    DropdownMenuItem(value: 'retour', child: Text('Retour')),
                  ],
                  onChanged: (v) => setState(() => _sens = v ?? _sens),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: TextField(
                  controller: _heureHab,
                  decoration: const InputDecoration(labelText: 'Départ (08h33)'),
                  onSubmitted: (_) => _ajouterHabituel(),
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          BoutonBz(label: 'Ajouter', pleineLargeur: true, onPressed: _ajouterHabituel),
        ],
      ),
    );
  }

  Widget _carteAttestation() {
    final st = _statut;
    final retard = n(st?['retard_min']);
    final supp = n(st?['suppressions']);
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const TitreSection('Mon attestation Garantie Fiabilité'),
          if (st != null) ...[
            Text.rich(
              TextSpan(
                style: const TextStyle(fontSize: 14.5, height: 1.4, color: Bz.encre2),
                children: [
                  TextSpan(text: '${s(st['mois_libelle'])} : '),
                  TextSpan(text: '$retard min', style: const TextStyle(fontWeight: FontWeight.w800, color: Bz.encre)),
                  const TextSpan(text: ' de retard validées, '),
                  TextSpan(text: '$supp', style: const TextStyle(fontWeight: FontWeight.w800, color: Bz.encre)),
                  TextSpan(text: ' suppression${supp > 1 ? 's' : ''}. ${s(st['conseil_gf'])}'),
                ],
              ),
            ),
            const SizedBox(height: 12),
          ],
          if (_mois.isEmpty)
            const Text('Aucun mois disponible pour l\'instant.', style: TextStyle(color: Bz.encre3))
          else ...[
            DropdownButtonFormField<String>(
              initialValue: _moisChoisi,
              dropdownColor: const Color(0xFF1A2440),
              decoration: const InputDecoration(labelText: 'Mois'),
              items: [for (final x in _mois) DropdownMenuItem(value: s(x['code']), child: Text(s(x['libelle'])))],
              onChanged: (v) => setState(() => _moisChoisi = v),
            ),
            const SizedBox(height: 12),
            BoutonBz(
              label: _pdfEnCours ? 'Préparation…' : 'Télécharger le PDF',
              icone: Icons.picture_as_pdf_rounded,
              pleineLargeur: true,
              onPressed: _pdfEnCours ? null : _pdf,
            ),
          ],
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(child: BoutonBz(label: 'Kit réclamation', variante: VarianteBouton.verre, onPressed: _kit)),
              const SizedBox(width: 10),
              Expanded(child: BoutonBz(label: 'Mes incidents', variante: VarianteBouton.verre, onPressed: _incidents)),
            ],
          ),
        ],
      ),
    );
  }

  Widget _carteGreve(Json g) {
    final jours = lm(g['jours']);
    if (jours.isEmpty) return const SizedBox.shrink();
    final auto = jours.any((j) => s(j['mode']) == 'auto');
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const TitreSection('Grève & plans de transport'),
          for (final j in jours) ...[
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Flexible(child: Text(dateFr(s(j['date'])), style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15.5))),
                      const SizedBox(width: 8),
                      Etiquette(
                        s(j['mode']) == 'veille' ? 'veille' : (s(j['source']) == 'officiel' ? 'grille officielle' : 'détection auto'),
                        couleur: s(j['mode']) == 'veille' ? Bz.encre2 : (n(j['nb_supprimes']) > 0 ? Bz.rouge : Bz.ok),
                      ),
                    ],
                  ),
                  if (s(j['mode']) == 'veille')
                    const Padding(padding: EdgeInsets.only(top: 4), child: Text('Aucun mouvement social annoncé.', style: TextStyle(color: Bz.encre2, fontSize: 13.5)))
                  else
                    for (final sg in lm(j['segments'])) _segmentGreve(sg),
                ],
              ),
            ),
          ],
          if (auto)
            const Mention('Détection automatique : la grille s\'affine au fil des annonces SNCF. Un train annoncé maintenu peut encore changer.'),
        ],
      ),
    );
  }

  Widget _segmentGreve(Json sg) {
    final trains = lm(sg['trains']);
    final supp = n(sg['nb_supprimes']);
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '${s(sg['gare_depart'])} → ${s(sg['gare_arrivee'])}${s(sg['fenetre']).isNotEmpty ? ' · ${s(sg['fenetre'])}' : ''} · $supp supprimé${supp > 1 ? 's' : ''} / ${trains.length}',
            style: const TextStyle(color: Bz.encre2, fontSize: 13.5, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 6),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final t in trains)
                Builder(builder: (_) {
                  final st = s(t['statut']);
                  final c = st == 'supprime' ? Bz.rouge : ((st == 'maintenu' || st == 'circule') ? Bz.ok : Bz.encre2);
                  return Container(
                    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                    decoration: BoxDecoration(
                      color: c.withValues(alpha: 0.14),
                      borderRadius: BorderRadius.circular(99),
                      border: Border.all(color: c.withValues(alpha: 0.5)),
                    ),
                    child: Text('${s(t['heure'])}${st == 'supprime' ? ' ✕' : ''}', style: Bz.chiffres.copyWith(fontSize: 13, fontWeight: FontWeight.w600, color: c)),
                  );
                }),
            ],
          ),
        ],
      ),
    );
  }

  Widget _carteCompte() {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const TitreSection('Mon compte'),
          BoutonBz(label: 'Exporter mes données', variante: VarianteBouton.verre, pleineLargeur: true, onPressed: _exporter),
          const SizedBox(height: 10),
          BoutonBz(label: 'Se déconnecter', variante: VarianteBouton.verre, pleineLargeur: true, onPressed: () => widget.onDeconnexion('')),
          const SizedBox(height: 10),
          BoutonBz(label: 'Supprimer mon compte', variante: VarianteBouton.danger, pleineLargeur: true, onPressed: _supprimer),
          const Mention('Bazo est un service indépendant de voyageurs, sans lien avec SNCF ni CFL. Données hébergées en France, jamais revendues.'),
        ],
      ),
    );
  }
}

class _FormProfil extends StatefulWidget {
  const _FormProfil({required this.api, required this.profil, required this.gares});

  final BazoApi api;
  final Json profil;
  final List<String> gares;

  @override
  State<_FormProfil> createState() => _FormProfilState();
}

class _FormProfilState extends State<_FormProfil> {
  late final _prenom = TextEditingController(text: s(widget.profil['prenom']) == 'Voyageur' ? '' : s(widget.profil['prenom']));
  late String _gd = s(widget.profil['gare_depart']);
  late String _gr = s(widget.profil['gare_retour']);
  late String _h = _tranche(s(widget.profil['heures']));
  late String _hr = _tranche(s(widget.profil['heures_retour']));
  bool _envoi = false;
  String? _erreur;

  String _tranche(String v) => _tranches.contains(v) ? v : _tranches[2];

  @override
  void dispose() {
    _prenom.dispose();
    super.dispose();
  }

  Future<void> _enregistrer() async {
    setState(() {
      _envoi = true;
      _erreur = null;
    });
    try {
      final p = _prenom.text.trim();
      await widget.api.majProfil({
        'prenom': p.isEmpty ? null : p,
        'gare_depart': _gd,
        'gare_retour': _gr,
        'heures': _h,
        'heures_retour': _hr,
      });
      if (mounted) Navigator.of(context).pop(true);
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
        _erreur = 'Enregistrement impossible.';
        _envoi = false;
        });
      }
    }
  }

  Widget _liste(String label, String valeur, List<String> items, ValueChanged<String> onChanged) {
    return DropdownButtonFormField<String>(
      initialValue: items.contains(valeur) ? valeur : null,
      isExpanded: true,
      dropdownColor: const Color(0xFF1A2440),
      decoration: InputDecoration(labelText: label),
      items: [for (final e in items) DropdownMenuItem(value: e, child: Text(e, overflow: TextOverflow.ellipsis))],
      onChanged: (v) => onChanged(v ?? valeur),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('Mon profil', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: -0.4)),
        const SizedBox(height: 14),
        TextField(controller: _prenom, maxLength: 40, decoration: const InputDecoration(labelText: 'Prénom', counterText: '')),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(child: _liste('Gare de départ', _gd, widget.gares, (v) => setState(() => _gd = v))),
            const SizedBox(width: 12),
            Expanded(child: _liste('Créneau aller', _h, _tranches, (v) => setState(() => _h = v))),
          ],
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(child: _liste('Gare d\'arrivée', _gr, widget.gares, (v) => setState(() => _gr = v))),
            const SizedBox(width: 12),
            Expanded(child: _liste('Créneau retour', _hr, _tranches, (v) => setState(() => _hr = v))),
          ],
        ),
        if (_erreur != null)
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: Text(_erreur!, style: const TextStyle(color: Bz.rouge, fontWeight: FontWeight.w600)),
          ),
        const SizedBox(height: 16),
        BoutonBz(label: _envoi ? 'Enregistrement…' : 'Enregistrer', pleineLargeur: true, onPressed: _envoi ? null : _enregistrer),
      ],
    );
  }
}
