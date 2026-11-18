import 'dart:async';
import 'dart:math' as math;
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import 'theme.dart';

/// Grain de film : une petite image de bruit générée une seule fois,
/// répétée en mosaïque à très faible opacité. Donne de la matière au verre.
class GrainTexture {
  const GrainTexture._();

  static ui.Image? _image;
  static Future<ui.Image>? _loading;

  static Future<ui.Image> charger() {
    if (_image != null) return Future.value(_image);
    return _loading ??= _generer();
  }

  static Future<ui.Image> _generer() async {
    const taille = 128;
    final rnd = math.Random(2027);
    final pixels = Uint8List(taille * taille * 4);
    for (var i = 0; i < taille * taille; i++) {
      final v = rnd.nextInt(256);
      pixels[i * 4] = v;
      pixels[i * 4 + 1] = v;
      pixels[i * 4 + 2] = v;
      pixels[i * 4 + 3] = 40 + rnd.nextInt(40);
    }
    final c = Completer<ui.Image>();
    ui.decodeImageFromPixels(pixels, taille, taille, ui.PixelFormat.rgba8888, c.complete);
    _image = await c.future;
    return _image!;
  }
}

/// Superpose le grain sur son enfant, sans jamais intercepter les gestes.
class GrainOverlay extends StatefulWidget {
  const GrainOverlay({super.key, this.opacite = 0.07, this.enfant});
  final double opacite;
  final Widget? enfant;

  @override
  State<GrainOverlay> createState() => _GrainOverlayState();
}

class _GrainOverlayState extends State<GrainOverlay> {
  ui.Image? _img;

  @override
  void initState() {
    super.initState();
    GrainTexture.charger().then((i) {
      if (mounted) setState(() => _img = i);
    });
  }

  @override
  Widget build(BuildContext context) {
    return Stack(
      fit: StackFit.passthrough,
      children: [
        if (widget.enfant != null) widget.enfant!,
        if (_img != null)
          Positioned.fill(
            child: IgnorePointer(
              child: CustomPaint(painter: _GrainPainter(_img!, widget.opacite)),
            ),
          ),
      ],
    );
  }
}

class _GrainPainter extends CustomPainter {
  _GrainPainter(this.image, this.opacite);
  final ui.Image image;
  final double opacite;

  @override
  void paint(Canvas canvas, Size size) {
    final shader = ImageShader(
      image,
      TileMode.repeated,
      TileMode.repeated,
      Matrix4.identity().storage,
    );
    final p = Paint()
      ..shader = shader
      ..color = Colors.white.withValues(alpha: opacite)
      ..blendMode = BlendMode.overlay;
    canvas.drawRect(Offset.zero & size, p);
  }

  @override
  bool shouldRepaint(_GrainPainter old) => old.image != image || old.opacite != opacite;
}

/// Fond « aurore » : trois halos flous (indigo, émeraude, violet) sur la nuit.
/// Ils dérivent très lentement pour que le verre ait toujours quelque chose à flouter.
class FondAurore extends StatefulWidget {
  const FondAurore({super.key, required this.enfant});
  final Widget enfant;

  @override
  State<FondAurore> createState() => _FondAuroreState();
}

class _FondAuroreState extends State<FondAurore> with SingleTickerProviderStateMixin {
  late final AnimationController _c =
      AnimationController(vsync: this, duration: const Duration(seconds: 28))..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Stack(
      fit: StackFit.expand,
      children: [
        const ColoredBox(color: Bz.fond),
        RepaintBoundary(
          child: AnimatedBuilder(
            animation: _c,
            builder: (_, __) => CustomPaint(painter: _AurorePainter(_c.value)),
          ),
        ),
        GrainOverlay(opacite: 0.10, enfant: widget.enfant),
      ],
    );
  }
}

class _AurorePainter extends CustomPainter {
  _AurorePainter(this.t);
  final double t;

  void _halo(Canvas c, Size s, Offset centre, double rayon, Color couleur) {
    final p = Paint()
      ..shader = ui.Gradient.radial(
        centre,
        rayon,
        [couleur.withValues(alpha: 0.34), couleur.withValues(alpha: 0)],
      );
    c.drawCircle(centre, rayon, p);
  }

  @override
  void paint(Canvas canvas, Size size) {
    final a = t * 2 * math.pi;
    _halo(
      canvas,
      size,
      Offset(size.width * (0.18 + 0.06 * math.sin(a)), size.height * (0.12 + 0.04 * math.cos(a))),
      size.width * 0.85,
      const Color(0xFF4F6BFF),
    );
    _halo(
      canvas,
      size,
      Offset(size.width * (0.92 + 0.05 * math.cos(a)), size.height * (0.45 + 0.05 * math.sin(a))),
      size.width * 0.75,
      const Color(0xFF10B981),
    );
    _halo(
      canvas,
      size,
      Offset(size.width * (0.25 + 0.06 * math.cos(a + 1)), size.height * (0.95 + 0.03 * math.sin(a))),
      size.width * 0.9,
      const Color(0xFF8B5CF6),
    );
  }

  @override
  bool shouldRepaint(_AurorePainter old) => old.t != t;
}
