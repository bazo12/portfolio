typedef Json = Map<String, dynamic>;

const _jours = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche'];
const _mois = [
  'janvier',
  'février',
  'mars',
  'avril',
  'mai',
  'juin',
  'juillet',
  'août',
  'septembre',
  'octobre',
  'novembre',
  'décembre',
];

/// « 2026-11-10 » → « mardi 10 novembre » (équivalent de `dateFr` de la PWA).
String dateFr(String? iso) {
  if (iso == null || iso.length < 10) return '';
  try {
    final d = DateTime(int.parse(iso.substring(0, 4)), int.parse(iso.substring(5, 7)), int.parse(iso.substring(8, 10)));
    return '${_jours[d.weekday - 1]} ${d.day} ${_mois[d.month - 1]}';
  } catch (_) {
    return iso;
  }
}

/// « 2026-11-10 » → « 10/11 ».
String jourMois(String? iso) {
  if (iso == null || iso.length < 10) return '';
  return '${iso.substring(8, 10)}/${iso.substring(5, 7)}';
}

String s(Object? v, [String defaut = '']) => v == null ? defaut : v.toString();

int n(Object? v, [int defaut = 0]) => v is num ? v.toInt() : (int.tryParse('$v') ?? defaut);

Map<String, dynamic> m(Object? v) => v is Map ? v.cast<String, dynamic>() : <String, dynamic>{};

List<Map<String, dynamic>> lm(Object? v) =>
    v is List ? v.whereType<Map>().map((e) => e.cast<String, dynamic>()).toList() : <Map<String, dynamic>>[];

String euros(num v) => v.toStringAsFixed(2).replaceAll('.', ',');

/// Date du jour à Paris, au format ISO (les dates métier sont celles de Paris).
String aujourdhuiIso() {
  final u = DateTime.now().toUtc();
  // Heure de Paris : UTC+1 l'hiver, UTC+2 l'été (dernier dimanche de mars → dernier dimanche d'octobre).
  final p = u.add(Duration(hours: _decalageParis(u)));
  return '${p.year.toString().padLeft(4, '0')}-${p.month.toString().padLeft(2, '0')}-${p.day.toString().padLeft(2, '0')}';
}

int _decalageParis(DateTime u) {
  DateTime dernierDimanche(int annee, int mois) {
    var d = DateTime.utc(annee, mois + 1, 0, 1);
    while (d.weekday != DateTime.sunday) {
      d = d.subtract(const Duration(days: 1));
    }
    return d;
  }

  final debut = dernierDimanche(u.year, 3);
  final fin = dernierDimanche(u.year, 10);
  return (u.isAfter(debut) && u.isBefore(fin)) ? 2 : 1;
}
