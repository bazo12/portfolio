import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'bazo_api.dart';
import 'glass.dart';
import 'session.dart';
import 'theme.dart';

/// Écran de connexion : ID Telegram + jeton obtenu avec la commande `/token` du bot.
class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key, required this.onConnecte, this.message});

  final void Function(BazoApi api) onConnecte;

  /// Message à afficher (session expirée, compte supprimé…).
  final String? message;

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _idCtrl = TextEditingController();
  final _tokenCtrl = TextEditingController();
  String? _erreur;
  bool _envoi = false;
  bool _masque = true;

  @override
  void dispose() {
    _idCtrl.dispose();
    _tokenCtrl.dispose();
    super.dispose();
  }

  Future<void> _connecter() async {
    final id = _idCtrl.text.trim();
    final jeton = _tokenCtrl.text.trim();
    if (id.isEmpty || jeton.isEmpty) {
      setState(() => _erreur = 'Renseigne ton ID Telegram et ton jeton.');
      return;
    }
    setState(() {
      _erreur = null;
      _envoi = true;
    });
    try {
      final api = BazoApi(telegramId: id, apiToken: jeton);
      await api.checkAuth();
      await Session.ecrire(api.telegramId, api.apiToken);
      HapticFeedback.mediumImpact();
      if (mounted) widget.onConnecte(api);
    } on ApiException catch (e) {
      if (mounted) setState(() => _erreur = e.message);
    } catch (_) {
      if (mounted) setState(() => _erreur = 'Connexion impossible. Vérifie ton réseau, ton ID et ton jeton.');
    } finally {
      if (mounted) setState(() => _envoi = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(20),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 440),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Text('Bazo', textAlign: TextAlign.center, style: TextStyle(fontSize: 46, fontWeight: FontWeight.w900, letterSpacing: -1.5)),
                  const SizedBox(height: 6),
                  const Text(
                    'Le Sillon lorrain en direct.\nTes retards, ton attestation, ton entraide.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Bz.encre2, fontSize: 15.5, height: 1.4),
                  ),
                  const SizedBox(height: 28),
                  GlassCard(
                    fort: true,
                    child: AutofillGroup(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          if (widget.message != null && widget.message!.isNotEmpty) ...[
                            Text(widget.message!, style: const TextStyle(color: Bz.ambre, fontWeight: FontWeight.w600)),
                            const SizedBox(height: 12),
                          ],
                          TextField(
                            controller: _idCtrl,
                            keyboardType: TextInputType.number,
                            textInputAction: TextInputAction.next,
                            decoration: const InputDecoration(labelText: 'ID Telegram'),
                          ),
                          const SizedBox(height: 12),
                          TextField(
                            controller: _tokenCtrl,
                            obscureText: _masque,
                            autocorrect: false,
                            enableSuggestions: false,
                            onSubmitted: (_) => _connecter(),
                            decoration: InputDecoration(
                              labelText: 'Jeton API (commande /token)',
                              suffixIcon: IconButton(
                                icon: Icon(_masque ? Icons.visibility_rounded : Icons.visibility_off_rounded, color: Bz.encre2),
                                onPressed: () => setState(() => _masque = !_masque),
                              ),
                            ),
                          ),
                          if (_erreur != null)
                            Padding(
                              padding: const EdgeInsets.only(top: 12),
                              child: Text(_erreur!, style: const TextStyle(color: Bz.rouge, fontWeight: FontWeight.w600)),
                            ),
                          const SizedBox(height: 18),
                          BoutonBz(label: _envoi ? 'Connexion…' : 'Se connecter', pleineLargeur: true, onPressed: _envoi ? null : _connecter),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 16),
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 12),
                    child: Text(
                      'Envoie /token au bot Bazo sur Telegram pour obtenir ton jeton. Il reste stocké de façon sécurisée sur cet appareil '
                      '(Keychain iOS / Keystore Android).',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: Bz.encre3, fontSize: 12.5, height: 1.4),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
