import 'package:flutter/foundation.dart';

import 'bazo_api.dart';

/// Phase 7 : point d'accroche des notifications push.
///
/// L'API sait déjà enregistrer un jeton (`POST /v1/devices/{id}`, via
/// `BazoApi.registerDevice`). Ce qui manque est le fournisseur du jeton natif :
/// FCM (Android) / APNs (iOS), qui demande un compte Firebase et un certificat Apple
/// côté propriétaire du projet. On n'ajoute donc volontairement aucune dépendance
/// Firebase ici et on ne touche pas au serveur.
///
/// Pour activer : au démarrage de l'application, assigner
/// `Push.fournisseur = () async => await FirebaseMessaging.instance.getToken();`
/// puis le reste est automatique (enregistrement après connexion et au retour au premier plan).
class Push {
  const Push._();

  /// Renvoie le jeton natif de l'appareil, ou `null` s'il n'est pas disponible.
  static Future<String?> Function()? fournisseur;

  static String get plateforme {
    switch (defaultTargetPlatform) {
      case TargetPlatform.iOS:
        return 'ios';
      case TargetPlatform.android:
        return 'android';
      default:
        return 'flutter';
    }
  }

  /// Enregistre le jeton auprès de l'API. Ne lève jamais : le push est un bonus,
  /// il ne doit pas bloquer l'application.
  static Future<bool> enregistrer(BazoApi api) async {
    final f = fournisseur;
    if (f == null) return false;
    try {
      final jeton = await f();
      if (jeton == null || jeton.length < 8) return false;
      await api.registerDevice(jeton, plateforme: plateforme);
      return true;
    } catch (_) {
      return false;
    }
  }
}
