# PhoneDeck

Android telefonu bilgisayardan kontrol eden masaüstü uygulaması (Python + PySide6). Telefona uygulama
kurmadan, root gerektirmeden, `adb` üzerinden çalışır.

[![tests](https://github.com/HuseyinKonak/PhoneDeck/actions/workflows/tests.yml/badge.svg)](https://github.com/HuseyinKonak/PhoneDeck/actions/workflows/tests.yml)

> **Uyarı:** Bu uygulama telefonda geniş yetkiyle çalışır: mesajları okur, SMS gönderir, dosya siler,
> ayar değiştirir. Yalnızca kendi telefonunda kullan. Ayrıntılar: [SECURITY.md](SECURITY.md).
> Proje eğitim amaçlıdır; kullanım sorumluluğu sana aittir ([Sorumluluk reddi](#sorumluluk-reddi)).

## Özellikler

**Genel**
- **Canlı ekran:** düşük gecikmeli ekran yansıtma; fare ile dokunma ve kaydırma, klavye ile yazma,
  Ctrl+sürükle ile yakınlaştırma, MP4 ekran kaydı, kalite ayarları (çözünürlük, bit hızı, FPS),
  dosya bırakarak gönderme / APK kurma
- **Genel Bakış:** model, Android sürümü, IP, batarya, depolama, bellek; kilitle, ekranı karart,
  kablosuz bağlan, yeniden başlat
- **Kontroller:** Wi-Fi, Bluetooth, mobil veri, otomatik döndürme, rahatsız etme, ses, parlaklık,
  medya tuşları, metin gönderme, bağlantı açma
- **Kamera ve Ses:** arka/ön kamerayı canlı izleme, fotoğraf ve video kaydı; mikrofonu ya da telefonun
  sesini bilgisayarda dinleme, WAV kaydı

**İletişim**
- **Bildirimler:** güncel bildirimler, oturum geçmişi, masaüstü uyarısı, telefondaki bildirimleri temizleme
- **Aramalar:** arama geçmişi (kullanılan SIM ile), numara çevirme, cevaplama, kapatma
- **Mesajlar:** konuşma görünümü, arama, CSV dışa aktarma; seçili SIM üzerinden doğrudan SMS gönderme
  ya da mesajı telefonda hazırlama
- **Rehber:** kişi arama, ekleme, düzenleme, silme; seçili SIM ile arama, CSV dışa aktarma
- **Çift SIM:** arama ve SMS için SIM seçimi

**Yönetim**
- **Uygulamalar:** gerçek adlar ve ikonlarla liste; aç, durdur, kaldır, APK yükle/çıkar, izinler,
  devre dışı bırakma, veri temizleme
- **Dosyalar:** gezinme, indirme, gönderme, yeniden adlandırma, kopyala/taşı, resim ve metin önizleme,
  sürükle-bırak
- **Sistem Ayarları:** bağlantı, ses, ekran, pil (simülasyon dahil), tarih/dil, gizlilik ve güvenlik
- **Ağ ve Wi-Fi:** Wi-Fi durumu, çevredeki ve kayıtlı ağlar, bağlanma/unutma, ping ve ad çözme
- **Cihazımı Bul:** çaldırma, titretme, kamera flaşını ve ekranı yakıp söndürme, hepsini aynı anda
  çalıştıran "Her Şey" düğmesi, son konum, kilitleme, kilit ekranı mesajı

**Geliştirici**
- **Terminal:** kalıcı kabuk oturumu (cd ve değişkenler korunur), canlı çıktı, kayıtlı komutlar
- **Logcat:** canlı sistem günlüğü; seviye, etiket, paket ve regex filtresi, dosyaya kaydetme
- **Performans:** CPU, bellek, pil sıcaklığı ve ağ grafikleri; çekirdek yükü; süreç tablosu
- **Geliştirici Araçları:** çözünürlük/DPI, koyu mod, animasyon ölçekleri, hata ayıklama bayrakları,
  güç menüsü, port yönlendirme, özellik ve ayar düzenleyici, intent başlatıcı

## Gereksinimler

- Windows 10/11 (uygulama Windows'ta geliştirildi ve yalnızca orada denendi)
- Python 3.10 veya üstü (3.14 ile geliştirildi)
- `adb` (Android platform-tools): PATH'te ya da Android SDK içinde
- USB hata ayıklaması açık bir Android telefon

Uygulama OPPO CPH2631 (Android 16, ColorOS) üzerinde denendi. Başka marka ve sürümlerde bazı özellikler
farklı davranabilir ya da çalışmayabilir; üreticiye özel ayarlar telefondan telefona değişir.

## Kurulum

### 1. adb

[Android platform-tools](https://developer.android.com/tools/releases/platform-tools) paketini indir,
bir klasöre aç ve o klasörü PATH'e ekle. Kontrol:

```
adb version
```

### 2. Telefon

1. **Ayarlar → Telefon hakkında → Yapı numarası**'na 7 kez dokun (Geliştirici seçenekleri açılır).
2. **Geliştirici seçenekleri → USB hata ayıklama**'yı aç.
3. Telefonu USB ile bağla, çıkan "USB hata ayıklamaya izin ver" penceresini onayla.
4. Xiaomi, OPPO gibi bazı üreticilerde dokunma ve ayar değiştirme için
   **"USB hata ayıklama (Güvenlik ayarları)"** ya da **"İzin izlemeyi devre dışı bırak"** da açılmalıdır.

Kontrol (telefon `device` olarak görünmeli):

```
adb devices
```

### 3. Uygulama

```
git clone https://github.com/HuseyinKonak/PhoneDeck.git
cd PhoneDeck
run.bat
```

`run.bat` ilk çalıştırmada sanal ortamı kurar ve bağımlılıkları indirir. Elle kurmak için:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m phonedeck
```

### Kablosuz bağlantı (isteğe bağlı)

Telefon USB ile bağlıyken **Genel Bakış → Kablosuz bağlan**'a bas; sonra kabloyu çıkarabilirsin.
Telefon ve bilgisayar aynı ağda olmalı. Yalnızca güvendiğin ağlarda kullan.

## Kısayollar (canlı ekran üzerinde)

| Girdi | İşlev |
|-------|-------|
| Sol tık / sürükle | Dokun / kaydır |
| Sağ tık, Esc | Geri |
| Orta tık | Ana ekran |
| Ctrl+sürükle | İki parmakla yakınlaştır |
| Ctrl+V | Bilgisayar panosunu telefona yapıştır |
| Dosya bırak | Telefona gönder (APK ise kur) |

## Güvenlik ve gizlilik

- Uygulama internete veri göndermez; telemetri ve hesap yoktur.
- SMS gönderme ve arama **ücretli olabilir ve geri alınamaz**.
- Dışa aktarılan CSV, kayıt ve fotoğraflar kişisel veridir; `.gitignore` bunları depoya almaz.
- İşin bitince USB hata ayıklamayı kapatman önerilir.

Tam liste ve güvenlik açığı bildirme: [SECURITY.md](SECURITY.md).

## Sorun giderme

| Belirti | Çözüm |
|---------|-------|
| "Cihaz yok" | Kabloyu ve `adb devices` çıktısını kontrol et; USB kipini "Dosya aktarımı" yap |
| "Telefonda USB hata ayıklama iznini onayla" | Telefonun ekranını aç, çıkan pencereyi onayla |
| Ekran görünüyor ama dokunma çalışmıyor | Geliştirici seçeneklerinde "USB hata ayıklama (Güvenlik ayarları)"nı aç |
| Ayar değişmiyor, "Permission denied" | Aynı güvenlik ayarı; bazı ayarlar üretici tarafından kilitlidir |
| Pil değerleri yanlış görünüyor | Sistem Ayarları → Pil → Sıfırla (simülasyon açık kalmış) |

## Geliştirme

```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests --cov=phonedeck.core
```

Testler sahte bir adb kullanır; telefon gerekmez. Arayüz (`phonedeck/ui`) otomatik testlerle kapsanmaz.

```
phonedeck/
  core/      telefonla konuşan mantık (Qt'ye bağlı değil, testli)
  core/scrcpy/  ekran, kamera ve ses akışı
  ui/        arayüz: ana pencere, canlı ekran, sayfalar
  assets/    scrcpy sunucusu ve telefondaki yardımcı (applister.jar)
tools/applister/   yardımcının Java kaynağı ve derleme betiği
tests/       çekirdek testleri
```

Katkı kuralları: [CONTRIBUTING.md](CONTRIBUTING.md).

### Notlar

- Ekran yansıtma için [scrcpy](https://github.com/Genymobile/scrcpy) sunucusu v3.3.3 kullanılır
  (`phonedeck/assets/scrcpy-server`, resmi sürümle aynı dosya). Protokol sürüme bağlıdır; dosyayı
  değiştirirsen `phonedeck/core/scrcpy/protocol.py` içindeki `SERVER_VERSION` da güncellenmeli.
- Uygulama adları ve ikonları, SIM seçerek SMS/arama, kamera flaşı ve bildirim temizleme telefonda
  `app_process` ile çalışan küçük bir yardımcıyla yapılır (`phonedeck/assets/applister.jar`).
  Kaynağı `tools/applister/`; yeniden derlemek için `tools/applister/build.ps1`
  (JDK ve Android SDK build-tools gerekir). Yardımcı gizli Android arayüzlerini kullandığı için
  Android sürümüne göre davranışı değişebilir.

## Sorumluluk reddi

Bu proje **eğitim ve kişisel kullanım amaçlıdır**.

- Yazılım "olduğu gibi" sunulur; hiçbir garanti verilmez (bkz. [LICENSE](LICENSE)).
- Kullanımdan doğan her türlü sonuç kullanıcının sorumluluğundadır: veri kaybı, silinen dosya ya da
  kişi, gönderilen SMS ve yapılan aramaların ücreti, bozulan ayar, açılmayan telefon dahil. Geliştirici
  bunlardan sorumlu tutulamaz.
- Ne yaptığını bilmediğin ayarı değiştirme. Özellikle Geliştirici Araçları, Sistem Ayarları, Terminal
  ve kurtarma/bootloader kipine yeniden başlatma telefonu kullanılamaz hale getirebilir.
- Uygulamayı yalnızca kendi cihazında ya da sahibinin açık izniyle kullan. Başkasının telefonuna,
  mesajlarına, kamerasına ya da mikrofonuna izinsiz erişmek suçtur; bu tür kullanımın hukuki
  sorumluluğu tamamen kullanana aittir.
- Önemli verilerini denemeden önce yedekle.

## Lisans

[MIT](LICENSE). Üçüncü taraf bileşenler: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Bu proje Google, Android ya da Genymobile ile bağlantılı değildir.
