import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Conservation de la session (identifiant Telegram + jeton) dans le coffre
/// du système : Keychain sur iOS, Keystore sur Android. Jamais en clair.
class Session {
  const Session._();

  static const _storage = FlutterSecureStorage();
  static const _kId = 'bazo_telegram_id';
  static const _kToken = 'bazo_api_token';

  static Future<({String id, String token})?> lire() async {
    try {
      final id = await _storage.read(key: _kId);
      final token = await _storage.read(key: _kToken);
      if (id == null || id.isEmpty || token == null || token.isEmpty) {
        return null;
      }
      return (id: id, token: token);
    } catch (_) {
      return null;
    }
  }

  static Future<void> ecrire(String id, String token) async {
    await _storage.write(key: _kId, value: id);
    await _storage.write(key: _kToken, value: token);
  }

  static Future<void> effacer() async {
    await _storage.delete(key: _kId);
    await _storage.delete(key: _kToken);
  }
}
