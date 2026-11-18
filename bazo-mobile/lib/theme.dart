import 'package:flutter/material.dart';

/// Palette Bazo : nuit profonde + verre dépoli.
/// Couleurs d'état imposées par le cahier des charges (docs/ARAGON_PROMPT.md).
class Bz {
  const Bz._();

  static const fond = Color(0xFF0F172A);
  static const fond2 = Color(0xFF111B33);
  static const ok = Color(0xFF10B981);
  static const ambre = Color(0xFFF59E0B);
  static const rouge = Color(0xFFFF2D4A);
  static const indigo = Color(0xFF8FA9FF);
  static const encre = Color(0xFFEEF1F8);
  static const encre2 = Color(0xFFB4BCD0);
  static const encre3 = Color(0xFF7F89A3);

  // Couleurs des trains sur la carte (identiques à la PWA).
  static const trainOk = Color(0xFF5EEAC0);
  static const trainRetard = Color(0xFFF5B840);

  /// Remplissage et bord du verre.
  static const verre = Color(0x14FFFFFF); // ~8 %
  static const verreFort = Color(0x24FFFFFF); // ~14 %
  static const bordVerre = Color(0x29FFFFFF); // ~16 %

  static Color pourStatut(String? statut) {
    switch (statut) {
      case 'supprime':
        return rouge;
      case 'retard':
        return ambre;
      default:
        return ok;
    }
  }

  static const rayon = 24.0;
  static const rayonPetit = 16.0;

  static const chiffres = TextStyle(fontFeatures: [FontFeature.tabularFigures()]);
}

ThemeData construireTheme() {
  const scheme = ColorScheme.dark(
    primary: Bz.indigo,
    onPrimary: Bz.fond,
    secondary: Bz.ok,
    surface: Bz.fond,
    onSurface: Bz.encre,
    error: Bz.rouge,
  );
  final base = ThemeData(
    useMaterial3: true,
    brightness: Brightness.dark,
    colorScheme: scheme,
    scaffoldBackgroundColor: Colors.transparent,
    splashFactory: InkSparkle.splashFactory,
  );
  return base.copyWith(
    textTheme: base.textTheme.apply(bodyColor: Bz.encre, displayColor: Bz.encre),
    appBarTheme: const AppBarTheme(
      backgroundColor: Colors.transparent,
      elevation: 0,
      scrolledUnderElevation: 0,
    ),
    dividerColor: Bz.bordVerre,
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: Bz.verre,
      labelStyle: const TextStyle(color: Bz.encre2),
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(Bz.rayonPetit),
        borderSide: const BorderSide(color: Bz.bordVerre),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(Bz.rayonPetit),
        borderSide: const BorderSide(color: Bz.bordVerre),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(Bz.rayonPetit),
        borderSide: const BorderSide(color: Bz.indigo, width: 1.5),
      ),
    ),
    switchTheme: SwitchThemeData(
      thumbColor: WidgetStateProperty.resolveWith(
        (s) => s.contains(WidgetState.selected) ? Bz.fond : Bz.encre2,
      ),
      trackColor: WidgetStateProperty.resolveWith(
        (s) => s.contains(WidgetState.selected) ? Bz.ok : Bz.verreFort,
      ),
      trackOutlineColor: const WidgetStatePropertyAll(Colors.transparent),
    ),
    snackBarTheme: const SnackBarThemeData(behavior: SnackBarBehavior.floating),
  );
}
