import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'accueil.dart';
import 'bazo_api.dart';
import 'entraide.dart';
import 'fil.dart';
import 'glass.dart';
import 'moi.dart';
import 'push.dart';
import 'theme.dart';
import 'trajet.dart';

/// Coque principale : 5 onglets comme la PWA (Accueil, Trajet, À bord, Entraide, Moi)
/// avec une barre flottante en verre dépoli.
class MainShell extends StatefulWidget {
  const MainShell({super.key, required this.api, required this.onDeconnexion});

  final BazoApi api;

  /// Efface la session et revient à la connexion (le texte est affiché sur l'écran de connexion).
  final void Function(String message) onDeconnexion;

  @override
  State<MainShell> createState() => _MainShellState();
}

class _MainShellState extends State<MainShell> with WidgetsBindingObserver {
  int _tab = 0;
  bool _pending = false;
  bool _sorti = false;

  static const _onglets = <({IconData icone, IconData pleine, String nom})>[
    (icone: Icons.home_outlined, pleine: Icons.home_rounded, nom: 'Accueil'),
    (icone: Icons.confirmation_number_outlined, pleine: Icons.confirmation_number_rounded, nom: 'Trajet'),
    (icone: Icons.sensors_outlined, pleine: Icons.sensors_rounded, nom: 'À bord'),
    (icone: Icons.volunteer_activism_outlined, pleine: Icons.volunteer_activism_rounded, nom: 'Entraide'),
    (icone: Icons.person_outline_rounded, pleine: Icons.person_rounded, nom: 'Moi'),
  ];

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    widget.api.onUnauthorized = () => _sortir('Session expirée : reconnecte-toi.');
    Push.enregistrer(widget.api);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    widget.api.onUnauthorized = null;
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) Push.enregistrer(widget.api);
  }

  void _sortir(String message) {
    if (_sorti) return;
    _sorti = true;
    widget.onDeconnexion(message);
  }

  void _aller(int i) {
    if (i == _tab) return;
    HapticFeedback.selectionClick();
    setState(() => _tab = i);
  }

  void _definirPending(bool v) {
    if (v != _pending && mounted) setState(() => _pending = v);
  }

  @override
  Widget build(BuildContext context) {
    final api = widget.api;
    return Scaffold(
      extendBody: true,
      body: Stack(
        children: [
          IndexedStack(
            index: _tab,
            children: [
              AccueilScreen(api: api, actif: _tab == 0, onAller: _aller, onPending: _definirPending),
              TrajetScreen(api: api, actif: _tab == 1, onPending: _definirPending, onCheckin: () => _aller(2)),
              FilScreen(api: api, actif: _tab == 2, onAller: _aller),
              EntraideScreen(api: api, actif: _tab == 3),
              MoiScreen(api: api, actif: _tab == 4, onDeconnexion: _sortir),
            ],
          ),
          Positioned(
            left: 16,
            right: 16,
            bottom: MediaQuery.of(context).padding.bottom + 10,
            child: _barre(),
          ),
        ],
      ),
    );
  }

  Widget _barre() {
    return ClipRRect(
      borderRadius: BorderRadius.circular(30),
      child: BackdropFilter(
        filter: ui.ImageFilter.blur(sigmaX: 28, sigmaY: 28),
        child: Container(
          padding: const EdgeInsets.all(6),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(30),
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [Colors.white.withValues(alpha: 0.16), Colors.white.withValues(alpha: 0.06)],
            ),
            border: Border.all(color: Bz.bordVerre),
            boxShadow: const [BoxShadow(color: Color(0x66000000), blurRadius: 30, offset: Offset(0, 12))],
          ),
          child: Row(
            children: [
              for (var i = 0; i < _onglets.length; i++) Expanded(child: _bouton(i)),
            ],
          ),
        ),
      ),
    );
  }

  Widget _bouton(int i) {
    final o = _onglets[i];
    final actif = i == _tab;
    final icone = Icon(actif ? o.pleine : o.icone, size: 24, color: actif ? Bz.encre : Bz.encre2);
    return Semantics(
      button: true,
      selected: actif,
      label: o.nom,
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: () => _aller(i),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 220),
          curve: Curves.easeOutCubic,
          padding: const EdgeInsets.symmetric(vertical: 8),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(24),
            color: actif ? Bz.indigo.withValues(alpha: 0.22) : Colors.transparent,
            border: Border.all(color: actif ? Bz.indigo.withValues(alpha: 0.5) : Colors.transparent),
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              i == 1 ? PointAlerte(enfant: icone, actif: _pending) : icone,
              const SizedBox(height: 3),
              Text(
                o.nom,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: 10.5, fontWeight: actif ? FontWeight.w800 : FontWeight.w600, color: actif ? Bz.encre : Bz.encre2),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
