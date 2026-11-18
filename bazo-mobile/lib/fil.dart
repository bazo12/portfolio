import 'package:flutter/material.dart';

import 'bazo_api.dart';
import 'fmt.dart';
import 'glass.dart';
import 'theme.dart';

/// Port de `chargerFil()` / `chargerSignalements()` : ce que les voyageurs
/// signalent sur les trains que tu as déclarés aujourd'hui.
class FilScreen extends StatefulWidget {
  const FilScreen({super.key, required this.api, required this.actif, required this.onAller});

  final BazoApi api;
  final bool actif;
  final ValueChanged<int> onAller;

  @override
  State<FilScreen> createState() => _FilScreenState();
}

class _FilScreenState extends State<FilScreen> {
  String _date = '';
  List<Json> _mes = [];
  String? _numero;
  String? _erreur;
  bool _chargeChips = true;

  Json? _signalements;
  Json? _types;
  bool _chargeSignalements = false;
  String? _erreurSignalements;
  final _occupes = <String>{};

  @override
  void initState() {
    super.initState();
    _chips();
  }

  @override
  void didUpdateWidget(FilScreen old) {
    super.didUpdateWidget(old);
    if (widget.actif && !old.actif) _chips();
  }

  Json? get _courant => _mes.where((x) => s(x['numero_train']) == _numero).firstOrNull;

  Future<void> _chips() async {
    try {
      final h = await widget.api.hub();
      if (!mounted) return;
      final mes = lm(h['mes_checkins']);
      setState(() {
        _date = s(h['date']);
        _mes = mes;
        _erreur = null;
        _chargeChips = false;
        if (mes.isNotEmpty && !mes.any((x) => s(x['numero_train']) == _numero)) {
          _numero = s(mes.first['numero_train']);
        }
      });
      if (mes.isNotEmpty) _charger();
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
        _erreur = e.message;
        _chargeChips = false;
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
        _erreur = 'Impossible de charger.';
        _chargeChips = false;
        });
      }
    }
  }

  Future<void> _charger() async {
    final c = _courant;
    if (c == null) return;
    setState(() {
      _chargeSignalements = true;
      _erreurSignalements = null;
    });
    try {
      final r = await Future.wait<Json>([
        widget.api.signalementsTrain(_date, s(c['numero_train'])),
        _types != null ? Future.value(_types!) : widget.api.typesSignalement(),
      ]);
      if (!mounted) return;
      setState(() {
        _signalements = r[0];
        _types = r[1];
      });
    } on ApiException catch (e) {
      if (mounted) setState(() => _erreurSignalements = e.message);
    } catch (_) {
      if (mounted) setState(() => _erreurSignalements = 'Impossible de charger.');
    } finally {
      if (mounted) setState(() => _chargeSignalements = false);
    }
  }

  Future<void> _voter(Json x, String vote) async {
    final cle = 'v${x['id']}$vote';
    setState(() => _occupes.add(cle));
    try {
      await widget.api.voter(n(x['id']), vote);
      if (!mounted) return;
      toast(context, 'Merci, c\'est noté.', ok: true);
      await _charger();
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message, erreur: true);
    } finally {
      if (mounted) setState(() => _occupes.remove(cle));
    }
  }

  Future<void> _inscrire(Json x) async {
    final ok = await confirmer(
      context,
      titre: 'C\'est bien ton trajet ?',
      texte: 'Ce signalement sera inscrit sur ton attestation comme déclaration du voyageur '
          '(distinct des données officielles). Il faut avoir réellement pris ce train.',
      ok: 'Oui, inscrire',
    );
    if (!ok || !mounted) return;
    try {
      await widget.api.inscrireSurAttestation(n(x['id']));
      if (!mounted) return;
      toast(context, 'Inscrit sur ton attestation.', ok: true);
      await _charger();
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message, erreur: true);
    }
  }

  Future<void> _signaler() async {
    final c = _courant;
    final types = _types;
    if (c == null || types == null) return;
    final choix = lm(types['types']).where((x) => s(x['code']) != 'je_suis_bloque').toList();
    final heure = s(c['heure_theo']);
    final numero = s(c['numero_train']);
    final choisi = await feuille<Json>(
      context,
      (ctx) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Que se passe-t-il ?', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: -0.4)),
          const SizedBox(height: 6),
          Text(
            'Train $numero · $heure. Choisis un motif : pas de texte libre, c\'est plus rapide et plus fiable.',
            style: const TextStyle(color: Bz.encre2, height: 1.35),
          ),
          const SizedBox(height: 16),
          for (final x in choix)
            Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: GlassCard(
                rayon: 16,
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
                onTap: () => Navigator.of(ctx).pop(x),
                child: Row(
                  children: [
                    Expanded(child: Text(s(x['libelle']), style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15.5))),
                    if (x['effet_pdf'] == true) const Etiquette('attestation', couleur: Bz.ambre),
                  ],
                ),
              ),
            ),
          Mention('${s(types['mention'])}. Les signalements « attestation » te seront proposés pour ton PDF après confirmation.'),
        ],
      ),
    );
    if (choisi == null || !mounted) return;
    String? valeur;
    if (choisi['valeur_requise'] == true) {
      valeur = await _demanderValeur();
      if (valeur == null || valeur.isEmpty) return;
    }
    try {
      final r = await widget.api.envoyerSignalement(
        date: _date,
        numeroTrain: numero,
        heureTheo: heure,
        codeType: s(choisi['code']),
        valeur: valeur,
      );
      if (!mounted) return;
      toast(context, r['existant'] == true ? 'Déjà signalé — merci.' : 'Signalement envoyé aux voyageurs.', ok: true);
      await _charger();
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message, erreur: true);
    }
  }

  Future<String?> _demanderValeur() {
    final ctrl = TextEditingController();
    return feuille<String>(
      context,
      (ctx) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Numéro de voie annoncé', style: TextStyle(fontSize: 21, fontWeight: FontWeight.w800)),
          const SizedBox(height: 6),
          const Text('Indique la voie telle qu\'elle est annoncée en gare (ex. 3).', style: TextStyle(color: Bz.encre2)),
          const SizedBox(height: 14),
          TextField(
            controller: ctrl,
            autofocus: true,
            keyboardType: TextInputType.text,
            maxLength: 6,
            decoration: const InputDecoration(labelText: 'Voie'),
          ),
          const SizedBox(height: 10),
          BoutonBz(label: 'Valider', pleineLargeur: true, onPressed: () => Navigator.of(ctx).pop(ctrl.text.trim())),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final bas = MediaQuery.of(context).padding.bottom + 108;
    final haut = MediaQuery.of(context).padding.top + 12;
    return RefreshIndicator(
      color: Bz.indigo,
      backgroundColor: const Color(0xFF1A2440),
      onRefresh: _chips,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(parent: BouncingScrollPhysics()),
        padding: EdgeInsets.fromLTRB(16, haut, 16, bas),
        children: [
          const EntetePage(
            titre: 'À bord',
            sous: 'Ce que les voyageurs signalent sur tes trains du jour. Information voyageurs, non vérifiée par SNCF.',
          ),
          ..._corps(),
        ],
      ),
    );
  }

  List<Widget> _corps() {
    if (_chargeChips) return const [Squelette()];
    if (_erreur != null) {
      return [GlassCard(child: VideBz(titre: 'Impossible de charger', texte: _erreur))];
    }
    if (_mes.isEmpty) {
      return [
        GlassCard(
          child: Column(
            children: [
              const VideBz(
                titre: 'Aucun train déclaré aujourd\'hui',
                texte: 'Déclare ton trajet pour suivre et partager ce qui se passe à bord.',
              ),
              BoutonBz(label: 'Déclarer mon trajet', pleineLargeur: true, onPressed: () => widget.onAller(1)),
            ],
          ),
        ),
      ];
    }
    return [
      SizedBox(
        height: 46,
        child: ListView(
          scrollDirection: Axis.horizontal,
          children: [
            for (final x in _mes)
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: _puce(x),
              ),
          ],
        ),
      ),
      const SizedBox(height: 12),
      _blocSignalements(),
    ];
  }

  Widget _puce(Json x) {
    final actif = s(x['numero_train']) == _numero;
    return GestureDetector(
      onTap: () {
        if (actif) return;
        setState(() {
          _numero = s(x['numero_train']);
          _signalements = null;
        });
        _charger();
      },
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        padding: const EdgeInsets.symmetric(horizontal: 16),
        alignment: Alignment.center,
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(99),
          color: actif ? Bz.indigo.withValues(alpha: 0.22) : Colors.white.withValues(alpha: 0.07),
          border: Border.all(color: actif ? Bz.indigo : Bz.bordVerre),
        ),
        child: Text(
          '${s(x['heure_theo'])} · ${s(x['numero_train'])}',
          style: Bz.chiffres.copyWith(fontWeight: FontWeight.w700, color: actif ? Bz.encre : Bz.encre2),
        ),
      ),
    );
  }

  Widget _blocSignalements() {
    final c = _courant;
    if (c == null) return const SizedBox.shrink();
    if (_chargeSignalements && _signalements == null) return const Squelette(lignes: 3);
    if (_erreurSignalements != null && _signalements == null) {
      return GlassCard(child: VideBz(titre: 'Impossible de charger', texte: _erreurSignalements));
    }
    final sg = _signalements;
    if (sg == null) return const SizedBox.shrink();
    final liste = lm(sg['signalements']);
    final libelles = {for (final t in lm(_types?['types'])) s(t['code']): s(t['libelle'])};
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          TitreSection('Train ${s(c['numero_train'])} · ${s(c['heure_theo'])}'),
          if (liste.isEmpty)
            const VideBz(titre: 'Rien à signaler pour l\'instant', texte: 'Tu es à bord ? Dis-le aux autres voyageurs.')
          else ...[
            const Text('Les autres voyageurs ont-ils raison ? Confirme ou conteste.', style: TextStyle(color: Bz.encre2, fontSize: 13.5)),
            const SizedBox(height: 4),
            for (var i = 0; i < liste.length; i++) ...[
              if (i > 0) const SeparateurBz(),
              _ligne(liste[i], libelles),
            ],
          ],
          const SizedBox(height: 14),
          BoutonBz(label: 'Signaler quelque chose', icone: Icons.campaign_rounded, pleineLargeur: true, onPressed: _types == null ? null : _signaler),
          Mention(s(sg['mention'])),
        ],
      ),
    );
  }

  Widget _ligne(Json x, Map<String, String> libelles) {
    final moi = s(x['id_auteur']) == widget.api.telegramId;
    final statut = s(x['statut']);
    final couleur = statut == 'confirme' ? Bz.ok : (statut == 'refute' ? Bz.rouge : Bz.ambre);
    final lib = statut == 'confirme' ? 'confirmé' : (statut == 'refute' ? 'contesté' : 'à confirmer');
    final titre = s(x['libelle']).isNotEmpty ? s(x['libelle']) : (libelles[s(x['code_type'])] ?? s(x['code_type']));
    return LigneBz(
      titre: titre,
      etiquette: Etiquette(lib, couleur: couleur),
      sous: '${n(x['nb_confirmations'])} confirment · ${n(x['nb_refutations'])} contestent${moi ? ' · toi' : ''}',
      enfantBas: moi
          ? (x['effet_pdf_possible'] == true
              ? Align(
                  alignment: Alignment.centerLeft,
                  child: BoutonBz(label: 'Inscrire sur mon attestation', petit: true, variante: VarianteBouton.verre, onPressed: () => _inscrire(x)),
                )
              : null)
          : Row(
              children: [
                BoutonBz(
                  label: 'Oui',
                  petit: true,
                  variante: VarianteBouton.ok,
                  onPressed: _occupes.contains('v${x['id']}confirme') ? null : () => _voter(x, 'confirme'),
                ),
                const SizedBox(width: 8),
                BoutonBz(
                  label: 'Non',
                  petit: true,
                  variante: VarianteBouton.danger,
                  onPressed: _occupes.contains('v${x['id']}refute') ? null : () => _voter(x, 'refute'),
                ),
              ],
            ),
    );
  }
}
