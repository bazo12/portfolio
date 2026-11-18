import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'bazo_api.dart';
import 'live_cache.dart';
import 'login.dart';
import 'session.dart';
import 'shell.dart';
import 'texture.dart';
import 'theme.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    statusBarIconBrightness: Brightness.light,
    statusBarBrightness: Brightness.dark,
    systemNavigationBarColor: Colors.transparent,
    systemNavigationBarIconBrightness: Brightness.light,
  ));
  runApp(const BazoApp());
}

class BazoApp extends StatelessWidget {
  const BazoApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Bazo',
      debugShowCheckedModeBanner: false,
      theme: construireTheme(),
      // Fond « aurore » + grain commun à tous les écrans : les panneaux en verre flottent dessus.
      builder: (context, child) => FondAurore(enfant: child ?? const SizedBox.shrink()),
      home: const _Racine(),
    );
  }
}

/// Décide quoi afficher : démarrage, connexion ou application.
/// La session (ID + jeton) est lue dans le Keychain / Keystore ; le jeton est revalidé au lancement.
class _Racine extends StatefulWidget {
  const _Racine();

  @override
  State<_Racine> createState() => _RacineState();
}

class _RacineState extends State<_Racine> {
  BazoApi? _api;
  String? _message;
  bool _pret = false;

  @override
  void initState() {
    super.initState();
    _reprendre();
  }

  Future<void> _reprendre() async {
    BazoApi? api;
    try {
      final saved = await Session.lire();
      if (saved != null) {
        final candidat = BazoApi(telegramId: saved.id, apiToken: saved.token);
        await candidat.checkAuth();
        api = candidat;
      }
    } catch (_) {
      // Jeton refusé, serveur injoignable ou stockage indisponible : retour à la connexion.
    }
    if (!mounted) return;
    setState(() {
      _api = api;
      _pret = true;
    });
  }

  Future<void> _deconnecter(String message) async {
    try {
      await Session.effacer();
      await LiveCache.effacer();
    } catch (_) {}
    if (!mounted) return;
    setState(() {
      _api = null;
      _message = message;
    });
  }

  @override
  Widget build(BuildContext context) {
    final Widget ecran;
    final api = _api;
    if (!_pret) {
      ecran = const Scaffold(key: ValueKey('demarrage'), body: Center(child: CircularProgressIndicator()));
    } else if (api == null) {
      ecran = LoginScreen(
        key: const ValueKey('login'),
        message: _message,
        onConnecte: (a) => setState(() {
          _api = a;
          _message = null;
        }),
      );
    } else {
      ecran = MainShell(key: ValueKey('shell-${api.telegramId}'), api: api, onDeconnexion: _deconnecter);
    }
    return AnimatedSwitcher(duration: const Duration(milliseconds: 280), child: ecran);
  }
}
