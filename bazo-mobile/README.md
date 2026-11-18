# Bazo Mobile

Client Flutter iOS / Android. Même métier que le bot : profil, trains, check-in, PDF.

```bash
flutter pub get
flutter run --dart-define=BAZO_API_URL=http://127.0.0.1:8080
```

Jeton Bearer via `/token` en message privé Telegram. L'ID et le jeton ne sont pas dans ce dépôt.

API : [../bazo](../bazo/). PWA sans store : `bazo/bazo_web`.
