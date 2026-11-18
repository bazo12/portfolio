import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// Dernier instantané de l'accueil, gardé sur l'appareil pour l'affichage hors ligne.
/// Aucune décision métier n'en est tirée : on affiche « données du … » et rien d'autre.
class LiveCache {
  const LiveCache._();

  static const _kLive = 'bazo_live_dernier';
  static const _kQuand = 'bazo_live_quand';

  static Future<void> ecrire(Map<String, dynamic> live) async {
    try {
      final p = await SharedPreferences.getInstance();
      await p.setString(_kLive, jsonEncode(live));
      await p.setInt(_kQuand, DateTime.now().millisecondsSinceEpoch);
    } catch (_) {
      // Le cache est un confort : une écriture ratée ne doit jamais gêner l'écran.
    }
  }

  static Future<({Map<String, dynamic> live, DateTime quand})?> lire() async {
    try {
      final p = await SharedPreferences.getInstance();
      final brut = p.getString(_kLive);
      final ms = p.getInt(_kQuand);
      if (brut == null || ms == null) return null;
      return (
        live: (jsonDecode(brut) as Map).cast<String, dynamic>(),
        quand: DateTime.fromMillisecondsSinceEpoch(ms),
      );
    } catch (_) {
      return null;
    }
  }

  static Future<void> effacer() async {
    try {
      final p = await SharedPreferences.getInstance();
      await p.remove(_kLive);
      await p.remove(_kQuand);
    } catch (_) {}
  }
}
