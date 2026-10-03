# Üçüncü taraf bileşenler

PhoneDeck'in kendi kodu [MIT lisansı](LICENSE) altındadır. Aşağıdaki bileşenler kendi lisanslarına tabidir.

## Depoda dağıtılan

| Bileşen | Dosya | Lisans | Kaynak |
|---------|-------|--------|--------|
| scrcpy sunucusu v3.3.3 | `phonedeck/assets/scrcpy-server` | Apache-2.0 ([metin](licenses/Apache-2.0.txt)) | https://github.com/Genymobile/scrcpy |

scrcpy'nin telif hakkı Genymobile ve Romain Vimont'a aittir. Dosya değiştirilmeden dağıtılır; resmi
v3.3.3 sürümündeki `scrcpy-server-v3.3.3` ile aynıdır
(SHA-256 `7e70323ba7f259649dd4acce97ac4fefbae8102b2c6d91e2e7be613fd5354be0`).

`phonedeck/assets/applister.jar` bu projenin kendi kodundan derlenir (`tools/applister/*.java`, MIT).

## Kurulumda indirilen (depoda yer almaz)

| Paket | Lisans | Kaynak |
|-------|--------|--------|
| PySide6 (Qt for Python) | LGPL-3.0 | https://pyside.org |
| PyAV | BSD-3-Clause | https://github.com/PyAV-Org/PyAV |
| FFmpeg (PyAV ile birlikte gelir) | LGPL/GPL (derlemeye göre) | https://ffmpeg.org |
| NumPy | BSD-3-Clause | https://numpy.org |

## Ayrıca gerekenler (kullanıcı kendisi kurar)

- Android platform-tools (`adb`): Android SDK lisans sözleşmesi, https://developer.android.com/tools/releases/platform-tools
