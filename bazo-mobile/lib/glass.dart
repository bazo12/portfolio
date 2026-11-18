import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'texture.dart';
import 'theme.dart';

/// Panneau de verre dépoli : flou d'arrière-plan, remplissage dégradé,
/// liseré lumineux et ombre portée douce.
class GlassCard extends StatelessWidget {
  const GlassCard({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(18),
    this.margin = EdgeInsets.zero,
    this.rayon = Bz.rayon,
    this.fort = false,
    this.teinte,
    this.onTap,
    this.flou = 22,
  });

  final Widget child;
  final EdgeInsetsGeometry padding;
  final EdgeInsetsGeometry margin;
  final double rayon;
  final bool fort;
  final Color? teinte;
  final VoidCallback? onTap;
  final double flou;

  @override
  Widget build(BuildContext context) {
    final haut = (fort ? Bz.verreFort : Bz.verre);
    final base = teinte ?? Colors.white;
    final br = BorderRadius.circular(rayon);
    final contenu = Container(
      padding: padding,
      decoration: BoxDecoration(
        borderRadius: br,
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            base.withValues(alpha: haut.a + 0.03),
            base.withValues(alpha: haut.a * 0.45),
          ],
        ),
        border: Border.all(color: Bz.bordVerre, width: 1),
      ),
      child: child,
    );
    return Container(
      margin: margin,
      decoration: BoxDecoration(
        borderRadius: br,
        boxShadow: const [
          BoxShadow(color: Color(0x40000000), blurRadius: 32, offset: Offset(0, 14)),
        ],
      ),
      child: ClipRRect(
        borderRadius: br,
        child: BackdropFilter(
          filter: ui.ImageFilter.blur(sigmaX: flou, sigmaY: flou),
          child: onTap == null
              ? contenu
              : Material(
                  color: Colors.transparent,
                  child: InkWell(onTap: onTap, child: contenu),
                ),
        ),
      ),
    );
  }
}

enum VarianteBouton { primaire, verre, ok, ambre, danger }

class BoutonBz extends StatelessWidget {
  const BoutonBz({
    super.key,
    required this.label,
    required this.onPressed,
    this.variante = VarianteBouton.primaire,
    this.petit = false,
    this.icone,
    this.pleineLargeur = false,
  });

  final String label;
  final VoidCallback? onPressed;
  final VarianteBouton variante;
  final bool petit;
  final IconData? icone;
  final bool pleineLargeur;

  Color get _couleur {
    switch (variante) {
      case VarianteBouton.ok:
        return Bz.ok;
      case VarianteBouton.ambre:
        return Bz.ambre;
      case VarianteBouton.danger:
        return Bz.rouge;
      case VarianteBouton.primaire:
        return Bz.indigo;
      case VarianteBouton.verre:
        return Colors.white;
    }
  }

  @override
  Widget build(BuildContext context) {
    final actif = onPressed != null;
    final c = _couleur;
    final plein = variante == VarianteBouton.primaire;
    final fg = plein ? Bz.fond : (variante == VarianteBouton.verre ? Bz.encre : c);
    final bouton = Opacity(
      opacity: actif ? 1 : 0.45,
      child: DecoratedBox(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(petit ? 12 : 16),
          gradient: plein
              ? const LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [Color(0xFFB4C4FF), Color(0xFF7C97FF)],
                )
              : null,
          color: plein ? null : c.withValues(alpha: variante == VarianteBouton.verre ? 0.10 : 0.14),
          border: plein
              ? null
              : Border.all(color: c.withValues(alpha: variante == VarianteBouton.verre ? 0.22 : 0.45)),
          boxShadow: plein
              ? [BoxShadow(color: Bz.indigo.withValues(alpha: 0.35), blurRadius: 18, offset: const Offset(0, 6))]
              : null,
        ),
        child: Material(
          color: Colors.transparent,
          child: InkWell(
            borderRadius: BorderRadius.circular(petit ? 12 : 16),
            onTap: onPressed == null
                ? null
                : () {
                    HapticFeedback.selectionClick();
                    onPressed!();
                  },
            child: Padding(
              padding: EdgeInsets.symmetric(horizontal: petit ? 14 : 20, vertical: petit ? 9 : 14),
              child: Row(
                mainAxisSize: pleineLargeur ? MainAxisSize.max : MainAxisSize.min,
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  if (icone != null) ...[
                    Icon(icone, size: petit ? 16 : 19, color: fg),
                    const SizedBox(width: 8),
                  ],
                  Flexible(
                    child: Text(
                      label,
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: fg,
                        fontWeight: FontWeight.w700,
                        fontSize: petit ? 13.5 : 15.5,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
    return pleineLargeur ? SizedBox(width: double.infinity, child: bouton) : bouton;
  }
}

/// Pastille d'état (fluide / retard / supprimé).
class PastilleStatut extends StatelessWidget {
  const PastilleStatut({super.key, required this.statut, required this.texte, this.petite = false});
  final String? statut;
  final String texte;
  final bool petite;

  @override
  Widget build(BuildContext context) {
    final c = Bz.pourStatut(statut);
    return Container(
      padding: EdgeInsets.symmetric(horizontal: petite ? 9 : 12, vertical: petite ? 4 : 6),
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.16),
        borderRadius: BorderRadius.circular(99),
        border: Border.all(color: c.withValues(alpha: 0.5)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 7,
            height: 7,
            decoration: BoxDecoration(
              color: c,
              shape: BoxShape.circle,
              boxShadow: [BoxShadow(color: c.withValues(alpha: 0.8), blurRadius: 8)],
            ),
          ),
          const SizedBox(width: 7),
          Flexible(
            child: Text(
              texte,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: c,
                fontWeight: FontWeight.w700,
                fontSize: petite ? 12 : 13.5,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class Etiquette extends StatelessWidget {
  const Etiquette(this.texte, {super.key, this.couleur = Bz.encre2});
  final String texte;
  final Color couleur;

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(left: 8),
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: couleur.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        texte,
        style: TextStyle(color: couleur, fontSize: 11.5, fontWeight: FontWeight.w700),
      ),
    );
  }
}

/// Ligne de liste : heure à gauche, titre + sous-titre, état à droite.
class LigneBz extends StatelessWidget {
  const LigneBz({
    super.key,
    this.heure,
    required this.titre,
    this.sous,
    this.droite,
    this.statutDroite,
    this.onTap,
    this.barre = false,
    this.enfantBas,
    this.etiquette,
  });

  final String? heure;
  final String titre;
  final String? sous;
  final String? droite;
  final String? statutDroite;
  final VoidCallback? onTap;
  final bool barre;
  final Widget? enfantBas;
  final Widget? etiquette;

  @override
  Widget build(BuildContext context) {
    final contenu = Padding(
      padding: const EdgeInsets.symmetric(vertical: 11),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (heure != null)
            SizedBox(
              width: 62,
              child: Text(
                heure!,
                style: Bz.chiffres.copyWith(
                  fontSize: 17,
                  fontWeight: FontWeight.w700,
                  decoration: barre ? TextDecoration.lineThrough : null,
                  color: barre ? Bz.encre3 : Bz.encre,
                ),
              ),
            ),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Flexible(
                      child: Text(
                        titre,
                        style: const TextStyle(fontSize: 15.5, fontWeight: FontWeight.w600),
                      ),
                    ),
                    if (etiquette != null) etiquette!,
                  ],
                ),
                if (sous != null && sous!.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(sous!, style: const TextStyle(color: Bz.encre2, fontSize: 13)),
                  ),
                if (enfantBas != null) Padding(padding: const EdgeInsets.only(top: 8), child: enfantBas),
              ],
            ),
          ),
          if (droite != null)
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: statutDroite == null
                  ? Text(droite!, style: const TextStyle(color: Bz.encre2, fontWeight: FontWeight.w600))
                  : PastilleStatut(statut: statutDroite, texte: droite!, petite: true),
            ),
        ],
      ),
    );
    if (onTap == null) return contenu;
    return InkWell(
      onTap: () {
        HapticFeedback.selectionClick();
        onTap!();
      },
      child: contenu,
    );
  }
}

class SeparateurBz extends StatelessWidget {
  const SeparateurBz({super.key});
  @override
  Widget build(BuildContext context) =>
      Container(height: 1, color: Colors.white.withValues(alpha: 0.07));
}

class TitreSection extends StatelessWidget {
  const TitreSection(this.texte, {super.key});
  final String texte;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(
          texte,
          style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w800, letterSpacing: -0.2),
        ),
      );
}

class EntetePage extends StatelessWidget {
  const EntetePage({super.key, required this.titre, this.sous});
  final String titre;
  final String? sous;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 8, 4, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              titre,
              style: const TextStyle(fontSize: 30, fontWeight: FontWeight.w800, letterSpacing: -0.8),
            ),
            if (sous != null)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(sous!, style: const TextStyle(color: Bz.encre2, fontSize: 14.5, height: 1.35)),
              ),
          ],
        ),
      );
}

class VideBz extends StatelessWidget {
  const VideBz({super.key, required this.titre, this.texte});
  final String titre;
  final String? texte;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 22, horizontal: 8),
        child: Column(
          children: [
            Text(titre, textAlign: TextAlign.center, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 16)),
            if (texte != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(texte!, textAlign: TextAlign.center, style: const TextStyle(color: Bz.encre2, height: 1.35)),
              ),
          ],
        ),
      );
}

class Mention extends StatelessWidget {
  const Mention(this.texte, {super.key});
  final String texte;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 12),
        child: Text(texte, style: const TextStyle(color: Bz.encre3, fontSize: 12.5, height: 1.4)),
      );
}

/// Squelette de chargement avec reflet qui glisse.
class Squelette extends StatefulWidget {
  const Squelette({super.key, this.lignes = 3});
  final int lignes;
  @override
  State<Squelette> createState() => _SqueletteState();
}

class _SqueletteState extends State<Squelette> with SingleTickerProviderStateMixin {
  late final AnimationController _c =
      AnimationController(vsync: this, duration: const Duration(milliseconds: 1400))..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final largeurs = [1.0, 0.72, 0.45, 0.6];
    return GlassCard(
      child: AnimatedBuilder(
        animation: _c,
        builder: (_, __) => Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            for (var i = 0; i < widget.lignes; i++)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: FractionallySizedBox(
                  widthFactor: largeurs[i % largeurs.length],
                  child: Container(
                    height: i == 0 ? 22 : 14,
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(8),
                      gradient: LinearGradient(
                        begin: Alignment(-1.5 + 3 * _c.value, 0),
                        end: Alignment(-0.5 + 3 * _c.value, 0),
                        colors: [
                          Colors.white.withValues(alpha: 0.05),
                          Colors.white.withValues(alpha: 0.16),
                          Colors.white.withValues(alpha: 0.05),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------- retours

void toast(BuildContext context, String message, {bool erreur = false, bool ok = false}) {
  if (erreur) {
    HapticFeedback.heavyImpact();
  } else if (ok) {
    HapticFeedback.lightImpact();
  }
  final c = erreur ? Bz.rouge : (ok ? Bz.ok : Bz.indigo);
  final m = ScaffoldMessenger.of(context);
  m.hideCurrentSnackBar();
  m.showSnackBar(
    SnackBar(
      backgroundColor: const Color(0xF21A2440),
      elevation: 0,
      margin: const EdgeInsets.fromLTRB(16, 0, 16, 96),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(18),
        side: BorderSide(color: c.withValues(alpha: 0.55)),
      ),
      duration: const Duration(milliseconds: 3200),
      content: Row(
        children: [
          Icon(
            erreur ? Icons.error_outline_rounded : (ok ? Icons.check_circle_outline_rounded : Icons.info_outline_rounded),
            color: c,
            size: 20,
          ),
          const SizedBox(width: 10),
          Expanded(child: Text(message, style: const TextStyle(color: Bz.encre, fontSize: 14.5))),
        ],
      ),
    ),
  );
}

/// Feuille modale en verre, qui monte depuis le bas.
Future<T?> feuille<T>(BuildContext context, Widget Function(BuildContext) builder) {
  return showModalBottomSheet<T>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: Colors.transparent,
    barrierColor: const Color(0x99050914),
    builder: (ctx) {
      final bas = MediaQuery.of(ctx).viewInsets.bottom;
      return Padding(
        padding: EdgeInsets.only(bottom: bas),
        child: ClipRRect(
          borderRadius: const BorderRadius.vertical(top: Radius.circular(28)),
          child: BackdropFilter(
            filter: ui.ImageFilter.blur(sigmaX: 30, sigmaY: 30),
            child: GrainOverlay(
              opacite: 0.08,
              enfant: Container(
                decoration: const BoxDecoration(
                  color: Color(0xCC141C33),
                  border: Border(top: BorderSide(color: Bz.bordVerre)),
                ),
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(20, 10, 20, 28),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Center(
                        child: Container(
                          width: 42,
                          height: 5,
                          margin: const EdgeInsets.only(bottom: 14),
                          decoration: BoxDecoration(
                            color: Colors.white.withValues(alpha: 0.25),
                            borderRadius: BorderRadius.circular(99),
                          ),
                        ),
                      ),
                      builder(ctx),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    },
  );
}

Future<bool> confirmer(
  BuildContext context, {
  required String titre,
  required String texte,
  String ok = 'Confirmer',
  VarianteBouton variante = VarianteBouton.primaire,
}) async {
  final r = await feuille<bool>(
    context,
    (ctx) => Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(titre, style: const TextStyle(fontSize: 21, fontWeight: FontWeight.w800, letterSpacing: -0.4)),
        const SizedBox(height: 8),
        Text(texte, style: const TextStyle(color: Bz.encre2, fontSize: 15, height: 1.4)),
        const SizedBox(height: 20),
        Row(
          children: [
            Expanded(
              child: BoutonBz(
                label: 'Annuler',
                variante: VarianteBouton.verre,
                onPressed: () => Navigator.of(ctx).pop(false),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: BoutonBz(label: ok, variante: variante, onPressed: () => Navigator.of(ctx).pop(true)),
            ),
          ],
        ),
      ],
    ),
  );
  return r ?? false;
}

/// Pastille numérique « point rouge » superposée à une icône.
class PointAlerte extends StatelessWidget {
  const PointAlerte({super.key, required this.enfant, required this.actif});
  final Widget enfant;
  final bool actif;
  @override
  Widget build(BuildContext context) => Stack(
        clipBehavior: Clip.none,
        children: [
          enfant,
          if (actif)
            Positioned(
              right: -3,
              top: -2,
              child: Container(
                width: 9,
                height: 9,
                decoration: BoxDecoration(
                  color: Bz.rouge,
                  shape: BoxShape.circle,
                  border: Border.all(color: Bz.fond, width: 1.5),
                ),
              ),
            ),
        ],
      );
}
