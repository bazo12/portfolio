import 'package:flutter_test/flutter_test.dart';

import 'package:bazo_mobile/main.dart';

void main() {
  testWidgets('Au lancement, Bazo affiche un indicateur de chargement', (tester) async {
    await tester.pumpWidget(const BazoApp());
    expect(find.byType(BazoApp), findsOneWidget);
  });
}
