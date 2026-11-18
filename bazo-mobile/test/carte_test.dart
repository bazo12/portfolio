import 'dart:io';
import 'dart:ui' as ui;

import 'package:bazo_mobile/carte_sillon.dart';
import 'package:bazo_mobile/fmt.dart';
import 'package:bazo_mobile/login.dart';
import 'package:bazo_mobile/texture.dart';
import 'package:bazo_mobile/theme.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';

/// Réseau factice au même format que `reseau` de GET /v1/live/{id}.
Json reseauFactice(int base) => {
      'serveur_ts': base,
      'generated_ts': base,
      'stations': [
        {'nom': 'Luxembourg', 'uic': 'L', 'lat': 49.60, 'lon': 6.13},
        {'nom': 'Thionville', 'uic': 'T', 'lat': 49.36, 'lon': 6.17},
        {'nom': 'Metz', 'uic': 'M', 'lat': 49.11, 'lon': 6.18},
        {'nom': 'Nancy', 'uic': 'N', 'lat': 48.69, 'lon': 6.17},
      ],
      'segments': [
        {'a': 'L', 'b': 'T', 'statut': 'ok', 'retard_max': 0, 'nb_trains': 2},
        {'a': 'T', 'b': 'M', 'statut': 'retard', 'retard_max': 12, 'nb_trains': 1},
        {'a': 'M', 'b': 'N', 'statut': 'supprime', 'retard_max': 0, 'nb_trains': 1},
      ],
      'trains': [
        {
          'numero': '88001',
          'sens': 'S',
          'statut': 'ok',
          'retard': 0,
          'stops': [
            ['L', base - 600, base - 540, 0, 0],
            ['T', base + 300, base + 360, 0, 0],
            ['M', base + 900, base + 960, 0, 0],
          ],
        },
        {
          'numero': '88002',
          'sens': 'S',
          'statut': 'retard',
          'retard': 12,
          'stops': [
            ['T', base - 300, base - 240, 0, 12],
            ['M', base + 600, base + 660, 0, 12],
            ['N', base + 1500, base + 1560, 0, 12],
          ],
        },
      ],
      'resume': {'statut': 'retard', 'nb_trains': 2, 'nb_retards': 1, 'nb_supprimes': 0},
    };

/// Enregistre une capture PNG de l'écran (aperçu visuel, sans comparaison : l'aurore est animée).
Future<void> apercu(WidgetTester tester, GlobalKey cle, String nom) async {
  await tester.runAsync(() async {
    final b = cle.currentContext!.findRenderObject()! as RenderRepaintBoundary;
    final img = await b.toImage(pixelRatio: 2);
    final data = await img.toByteData(format: ui.ImageByteFormat.png);
    Directory('test/apercu').createSync(recursive: true);
    File('test/apercu/$nom.png').writeAsBytesSync(data!.buffer.asUint8List());
  });
}

void main() {
  test('formatage : dates et conversions tolérantes', () {
    expect(dateFr('2026-11-10'), 'mardi 10 novembre');
    expect(jourMois('2026-11-10'), '10/11');
    expect(s(null), '');
    expect(n('12'), 12);
    expect(n(null), 0);
    expect(m(null), isEmpty);
    expect(lm(null), isEmpty);
  });

  test('géométrie : stations ordonnées, trains positionnés sur le tracé', () {
    const base = 1800000000;
    final geo = GeoCarte(reseauFactice(base), const Size(360, 220));
    expect(geo.exploitable, isTrue);
    expect(geo.chemins.length, 3);
    // Comme la PWA : x = latitude croissante. Luxembourg (nord) à droite, Nancy (sud) à gauche.
    expect(geo.pts.first.dx, greaterThan(geo.pts.last.dx));
    expect(geo.pts.last.dx, closeTo(34, 0.001));
    expect(geo.cumul.first, 0);
    expect(geo.cumul.last, greaterThan(300));

    final trains = lm(reseauFactice(base)['trains']);
    // Train 1 : parti de Luxembourg, pas encore à Thionville -> entre les deux.
    final a1 = geo.positionTrain(trains[0], base.toDouble())!;
    expect(a1, greaterThan(0));
    expect(a1, lessThan(geo.cumul[1]));
    // Train 2 : entre Thionville et Metz.
    final a2 = geo.positionTrain(trains[1], base.toDouble())!;
    expect(a2, greaterThan(geo.cumul[1]));
    expect(a2, lessThan(geo.cumul[2]));
    // Hors fenêtre de circulation -> pas de train affiché.
    expect(geo.positionTrain(trains[0], base + 100000.0), isNull);
    // Arrêt en gare -> position exacte de la gare.
    expect(geo.positionTrain(trains[0], base + 330.0), closeTo(geo.cumul[1], 0.001));
  });

  testWidgets('la carte se dessine avec des données et en l\'absence de réseau', (tester) async {
    final base = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    await tester.binding.setSurfaceSize(const Size(400, 300));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final cle = GlobalKey();
    await tester.pumpWidget(
      MaterialApp(
        theme: construireTheme(),
        home: RepaintBoundary(
          key: cle,
          child: FondAurore(
            enfant: Scaffold(
              body: Padding(
                padding: const EdgeInsets.all(16),
                child: SizedBox(
                  height: 240,
                  child: CarteSillon(live: {'reseau': reseauFactice(base)}, recuLe: DateTime.now()),
                ),
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 600));
    expect(tester.takeException(), isNull);
    await apercu(tester, cle, 'carte');

    await tester.pumpWidget(
      MaterialApp(
        theme: construireTheme(),
        home: Scaffold(body: SizedBox(height: 240, child: CarteSillon(live: const {'reseau': null}, recuLe: DateTime.fromMillisecondsSinceEpoch(0)))),
      ),
    );
    await tester.pump(const Duration(milliseconds: 100));
    expect(tester.takeException(), isNull);
  });

  testWidgets('l\'écran de connexion valide les champs vides', (tester) async {
    final cle = GlobalKey();
    await tester.binding.setSurfaceSize(const Size(400, 800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(
      MaterialApp(
        theme: construireTheme(),
        home: RepaintBoundary(
          key: cle,
          child: FondAurore(enfant: LoginScreen(onConnecte: (_) {})),
        ),
      ),
    );
    await tester.pump(const Duration(milliseconds: 500));
    await tester.tap(find.text('Se connecter'));
    await tester.pump();
    expect(find.text('Renseigne ton ID Telegram et ton jeton.'), findsOneWidget);
    await apercu(tester, cle, 'login');
  });
}
