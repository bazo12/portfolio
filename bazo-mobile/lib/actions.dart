import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'bazo_api.dart';
import 'glass.dart';

/// Même parcours que `genererPdf()` de la PWA : lien signé demandé au serveur,
/// puis ouverture dans le navigateur du système. Rien n'est recalculé ici.
Future<String?> telechargerPdf(BuildContext context, BazoApi api, String mois) async {
  try {
    final uri = await api.pdfUri(mois);
    final ok = await launchUrl(uri, mode: LaunchMode.externalApplication);
    if (!ok) return 'Impossible d\'ouvrir le PDF sur cet appareil.';
    return null;
  } on ApiException catch (e) {
    return e.status == 404 ? 'Aucun incident validé sur cette période : rien à attester.' : e.message;
  } catch (e) {
    return 'Impossible d\'ouvrir le PDF.';
  }
}

/// Raccourci : télécharge et affiche le résultat dans un toast.
Future<void> pdfAvecToast(BuildContext context, BazoApi api, String mois) async {
  toast(context, 'Préparation de l\'attestation…');
  final err = await telechargerPdf(context, api, mois);
  if (!context.mounted) return;
  if (err != null) {
    toast(context, err, erreur: true);
  } else {
    toast(context, 'Attestation prête.', ok: true);
  }
}
