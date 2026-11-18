import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'config.dart';

// Carte du Sillon : portée dans carte_sillon.dart (dessinerCarte / majTrains de bazo_web/app.js).
// Le JSON de GET /v1/live/{id} suffit : ne jamais recalculer retards ni PDF ici.

class BazoApi {
  BazoApi({required this.telegramId, required this.apiToken});
  final String telegramId;
  final String apiToken;

  /// Appelé quand l'API refuse le jeton (401 / 403) : l'application revient à la connexion.
  VoidCallback? onUnauthorized;

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $apiToken',
      };

  Uri _u(String path) => Uri.parse('$bazoApiBase$path');

  Future<void> checkAuth() async {
    final r = await http.get(_u('/v1/auth/check'), headers: _headers);
    if (r.statusCode != 200) throw Exception(r.body);
  }

  Future<Map<String, dynamic>> profil() async {
    final r = await http.get(_u('/v1/profil/$telegramId'), headers: _headers);
    if (r.statusCode != 200) throw Exception(r.body);
    return jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> statut([String? mois]) async {
    final q = mois != null ? '?mois=$mois' : '';
    final r = await http.get(_u('/v1/statut/$telegramId$q'), headers: _headers);
    if (r.statusCode != 200) throw Exception(r.body);
    return jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
  }

  Future<List<dynamic>> trains(String sens) async {
    final r = await http.get(
      _u('/v1/trains/$telegramId?sens=$sens'),
      headers: _headers,
    );
    if (r.statusCode != 200) throw Exception(r.body);
    return (jsonDecode(utf8.decode(r.bodyBytes)) as Map)['trains'] as List<dynamic>;
  }

  Future<void> checkin({
    required String sens,
    required String numeroTrain,
    required String heureTheo,
    String? heureArriveeTheo,
    String? destination,
    String? date,
  }) async {
    final r = await http.post(
      _u('/v1/checkin'),
      headers: _headers,
      body: jsonEncode({
        'id_telegram': telegramId,
        'sens': sens,
        'numero_train': numeroTrain,
        'heure_theo': heureTheo,
        'heure_arrivee_theo': heureArriveeTheo,
        'destination': destination,
        if (date != null) 'date': date,
      }),
    );
    if (r.statusCode != 200) throw Exception(r.body);
  }

  Future<Map<String, dynamic>> checkoutPending() async {
    final r = await http.get(_u('/v1/checkout/pending/$telegramId'), headers: _headers);
    if (r.statusCode != 200) throw Exception(r.body);
    return jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
  }

  Future<void> checkout(int trajetId, String action) async {
    final r = await http.post(
      _u('/v1/checkout/$trajetId'),
      headers: _headers,
      body: jsonEncode({'id_telegram': telegramId, 'action': action}),
    );
    if (r.statusCode != 200) throw Exception(r.body);
  }

  Future<String> pdfLink(String mois) async {
    final r = await http.post(
      _u('/v1/pdf/$telegramId/link?mois=$mois'),
      headers: _headers,
    );
    if (r.statusCode != 200) throw Exception(r.body);
    return (jsonDecode(utf8.decode(r.bodyBytes)) as Map)['url'] as String;
  }

  /// Lien de téléchargement signé, sur le même hôte que l'API (pas l'URL interne du serveur).
  Future<Uri> pdfDownloadUri(String mois) async {
    final r = await http.post(
      _u('/v1/pdf/$telegramId/link?mois=$mois'),
      headers: _headers,
    );
    if (r.statusCode != 200) throw Exception(r.body);
    final map = jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
    final token = map['token'] as String?;
    if (token == null || token.isEmpty) {
      throw Exception('Jeton PDF absent');
    }
    return _u('/v1/pdf/download').replace(queryParameters: {'token': token});
  }

  Future<Map<String, dynamic>> live() async {
    final r = await http.get(_u('/v1/live/$telegramId'), headers: _headers);
    if (r.statusCode != 200) throw Exception(r.body);
    return jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
  }

  Future<List<dynamic>> pdfMois() async {
    final r = await http.get(_u('/v1/pdf/mois/$telegramId'), headers: _headers);
    if (r.statusCode != 200) throw Exception(r.body);
    return (jsonDecode(utf8.decode(r.bodyBytes)) as Map)['mois'] as List<dynamic>;
  }

  Future<void> registerDevice(String token, {String plateforme = 'flutter'}) async {
    final r = await http.post(
      _u('/v1/devices/$telegramId'),
      headers: _headers,
      body: jsonEncode({'device_token': token, 'plateforme': plateforme}),
    );
    if (r.statusCode != 200) throw Exception(r.body);
  }

  Future<Map<String, dynamic>> reclamation([String? mois]) async {
    final q = mois != null ? '?mois=$mois' : '';
    final r = await http.get(_u('/v1/reclamation/$telegramId$q'), headers: _headers);
    if (r.statusCode != 200) throw Exception(r.body);
    return jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic>;
  }

  // ------------------------------------------------------------------
  // Routes ajoutées pour les écrans portés depuis la PWA (bazo_web/app.js).
  // Toutes lèvent ApiException avec un message prêt à afficher.
  // ------------------------------------------------------------------

  String get _id => Uri.encodeComponent(telegramId);

  Future<dynamic> _req(String method, String path, {Object? body}) async {
    final uri = _u(path);
    http.Response r;
    try {
      const timeout = Duration(seconds: 20);
      switch (method) {
        case 'POST':
          r = await http
              .post(uri, headers: _headers, body: body == null ? null : jsonEncode(body))
              .timeout(timeout);
        case 'PUT':
          r = await http
              .put(uri, headers: _headers, body: body == null ? null : jsonEncode(body))
              .timeout(timeout);
        case 'DELETE':
          r = await http.delete(uri, headers: _headers).timeout(timeout);
        default:
          r = await http.get(uri, headers: _headers).timeout(timeout);
      }
    } catch (_) {
      throw ApiException(0, 'Pas de connexion. Réessaie dans un instant.');
    }
    if (r.statusCode == 401 || r.statusCode == 403) onUnauthorized?.call();
    if (r.statusCode < 200 || r.statusCode >= 300) {
      throw ApiException(r.statusCode, _detail(r));
    }
    if (r.statusCode == 204 || r.bodyBytes.isEmpty) return null;
    return jsonDecode(utf8.decode(r.bodyBytes));
  }

  static String _detail(http.Response r) {
    try {
      final j = jsonDecode(utf8.decode(r.bodyBytes));
      if (j is Map) {
        final d = j['detail'];
        if (d is String && d.isNotEmpty) return d;
        if (d is List && d.isNotEmpty && d.first is Map) {
          final m = (d.first as Map)['msg'];
          if (m is String) return m;
        }
      }
    } catch (_) {}
    return 'Erreur ${r.statusCode}';
  }

  Future<Map<String, dynamic>> _map(String m, String p, {Object? body}) async =>
      ((await _req(m, p, body: body)) as Map).cast<String, dynamic>();

  /// Carte publique du réseau (sans profil).
  Future<Map<String, dynamic>> liveSillon() => _map('GET', '/v1/live/sillon');

  Future<Map<String, dynamic>> liveAccueil() => _map('GET', '/v1/live/$_id');

  Future<Map<String, dynamic>> statutMois() => _map('GET', '/v1/statut/$_id');

  Future<Map<String, dynamic>> pending() => _map('GET', '/v1/checkout/pending/$_id');

  Future<Map<String, dynamic>> checkoutAction(int trajetId, String action) =>
      _map('POST', '/v1/checkout/$trajetId', body: {'id_telegram': telegramId, 'action': action});

  Future<Map<String, dynamic>> trainsDuSens(String sens) => _map('GET', '/v1/trains/$_id?sens=$sens');

  Future<Map<String, dynamic>> propositions() => _map('GET', '/v1/propositions/$_id');

  Future<void> repondreProposition(int id, String action) =>
      _req('POST', '/v1/propositions/$_id/$id', body: {'action': action});

  Future<Map<String, dynamic>> profilComplet() => _map('GET', '/v1/profil/$_id');

  Future<void> majProfil(Map<String, dynamic> champs) => _req('PUT', '/v1/profil/$_id', body: champs);

  Future<List<String>> gares() async {
    final d = await _map('GET', '/v1/geo/gares?region=grand_est');
    return (d['gares'] as List).map((g) => (g as Map)['nom'] as String).toList();
  }

  Future<Map<String, dynamic>> habituels() => _map('GET', '/v1/habituels/$_id');

  Future<void> ajouterHabituel(String sens, String heure) =>
      _req('POST', '/v1/habituels/$_id', body: {'sens': sens, 'heure': heure});

  Future<void> retirerHabituel(String sens, String heure) => _req(
        'DELETE',
        '/v1/habituels/$_id?sens=$sens&heure=${Uri.encodeQueryComponent(heure)}',
      );

  Future<Map<String, dynamic>> greve() => _map('GET', '/v1/greve/$_id');

  Future<Map<String, dynamic>> pdfMoisDispo() => _map('GET', '/v1/pdf/mois/$_id');

  /// Lien de téléchargement signé du PDF (même hôte que l'API). Le PDF reste
  /// entièrement produit par le serveur : on ne génère rien ici.
  Future<Uri> pdfUri(String mois) async {
    final d = await _map('POST', '/v1/pdf/$_id/link?mois=${Uri.encodeQueryComponent(mois)}');
    final token = d['token'] as String?;
    if (token == null || token.isEmpty) throw ApiException(500, 'Lien du PDF indisponible.');
    return _u('/v1/pdf/download').replace(queryParameters: {'token': token});
  }

  Future<Map<String, dynamic>> kitReclamation() => _map('GET', '/v1/reclamation/$_id');

  Future<Map<String, dynamic>> incidents() => _map('GET', '/v1/incidents/$_id');

  Future<String> exportDonnees() async {
    final r = await http.get(_u('/v1/account/$_id/export'), headers: _headers);
    if (r.statusCode == 401 || r.statusCode == 403) onUnauthorized?.call();
    if (r.statusCode != 200) throw ApiException(r.statusCode, _detail(r));
    return utf8.decode(r.bodyBytes);
  }

  Future<void> supprimerCompte() => _req('DELETE', '/v1/account/$_id');

  // --- À bord (signalements)

  Future<Map<String, dynamic>> typesSignalement() => _map('GET', '/v1/signalements/types');

  Future<Map<String, dynamic>> signalementsTrain(String date, String numero) => _map(
        'GET',
        '/v1/trains/$date/${Uri.encodeComponent(numero)}/signalements?id_telegram=$_id',
      );

  Future<Map<String, dynamic>> envoyerSignalement({
    required String date,
    required String numeroTrain,
    required String heureTheo,
    required String codeType,
    String? valeur,
  }) =>
      _map('POST', '/v1/signalements', body: {
        'id_telegram': telegramId,
        'date': date,
        'numero_train': numeroTrain,
        'heure_theo': heureTheo,
        'code_type': codeType,
        'valeur': valeur,
      });

  Future<void> voter(int signalementId, String vote) => _req(
        'POST',
        '/v1/signalements/$signalementId/vote',
        body: {'id_telegram': telegramId, 'vote': vote},
      );

  Future<void> inscrireSurAttestation(int signalementId) => _req(
        'POST',
        '/v1/signalements/$signalementId/trajet',
        body: {'id_telegram': telegramId, 'action': 'inscrire'},
      );

  // --- Entraide (hub)

  Future<Map<String, dynamic>> hub() => _map('GET', '/v1/hub/$_id');

  Future<void> jeSuisBloque({
    required String date,
    required String numeroTrain,
    required String heureTheo,
    required String gare,
  }) =>
      _req('POST', '/v1/hub/$_id/bloque', body: {
        'date': date,
        'numero_train': numeroTrain,
        'heure_theo': heureTheo,
        'gare': gare,
      });

  Future<Map<String, dynamic>> publierOffre(Map<String, dynamic> offre) =>
      _map('POST', '/v1/hub/$_id/offres', body: offre);

  Future<Map<String, dynamic>> reserverPlace(int offreId) =>
      _map('POST', '/v1/hub/$_id/offres/$offreId/reserver');

  Future<void> annulerPlace(int offreId) => _req('DELETE', '/v1/hub/$_id/offres/$offreId/reserver');

  Future<void> statutOffre(int offreId, String statut) =>
      _req('POST', '/v1/hub/$_id/offres/$offreId/statut', body: {'statut': statut});

  Future<void> signalerOffre(int offreId) => _req(
        'POST',
        '/v1/hub/$_id/offres/$offreId/abus',
        body: {'motif': 'signalé depuis l\'application'},
      );
}

/// Erreur d'API avec un message prêt à afficher à l'utilisateur.
class ApiException implements Exception {
  ApiException(this.status, this.message);
  final int status;
  final String message;

  @override
  String toString() => message;
}
