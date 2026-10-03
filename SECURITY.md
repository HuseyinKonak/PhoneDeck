# Güvenlik

## Bu uygulama ne yapabilir

PhoneDeck, USB hata ayıklaması açık bir Android telefona `adb` üzerinden **kabuk (shell) kullanıcısı
yetkisiyle** erişir. Bu yetkiyle şunları yapabilir:

- Ekranı görme ve kontrol etme, kamera ve mikrofon akışını alma
- SMS, arama geçmişi, rehber ve bildirimleri okuma; rehberi değiştirme
- SMS gönderme ve arama başlatma (ücretli olabilir)
- Dosya okuma, yazma ve silme; uygulama kurma, kaldırma, verilerini temizleme
- Sistem ayarlarını değiştirme, yeniden başlatma

Bu yüzden uygulamayı **yalnızca kendi telefonunda** ya da sahibinin açık izni olan telefonlarda kullan.
Başkasının cihazına izinsiz erişmek çoğu ülkede suçtur.

## Veriler nereye gider

- Uygulama hiçbir veriyi internete göndermez; telemetri, hesap ya da sunucu yoktur.
- Trafik yalnızca bilgisayar ile telefon arasındadır (USB ya da yerel ağ üzerinden adb).
- Dışa aktardığın dosyalar (CSV, ekran kaydı, fotoğraf, ses) bilgisayarında, seçtiğin klasörde kalır.
  Bunlar kişisel veridir; depoya ya da başkalarına gönderme.
- Telefonda `/data/local/tmp/` altına yardımcı dosyalar kopyalanır (`phonedeck-*.jar`,
  `phonedeck-icons/`, `phonedeck-state/`). Silmek için:
  `adb shell rm -rf /data/local/tmp/phonedeck-*`

## Güvenli kullanım

- İşin bitince **USB hata ayıklamayı kapat**. Açık kaldığı sürece telefona bağlanan ve izin verilen her
  bilgisayar aynı yetkiye sahip olur.
- **Kablosuz adb** (`adb tcpip 5555`) yalnızca güvendiğin ağlarda kullanılmalı; ortak ağlarda kapat
  (`adb usb`). Aynı ağdaki başka bir cihaz bağlanmayı deneyebilir.
- Telefonda çıkan "USB hata ayıklamaya izin ver" penceresinde yalnızca kendi bilgisayarını onayla.
  Eski izinleri silmek için: Geliştirici seçenekleri → USB hata ayıklama yetkilerini iptal et.
- "Pil simülasyonu", çözünürlük/DPI ve benzeri test ayarlarını işin bitince sıfırla.

## Geri alınamayan işlemler

SMS gönderme, arama, kişi silme, dosya silme, uygulama verisi temizleme, uygulama kaldırma, APK kurma,
bildirimleri temizleme ve kurtarma/bootloader kipine yeniden başlatma geri alınamaz. Uygulama bunlardan
önce onay sorar; onay vermeden önce metni oku.

Onay **sorulmayan** durumlar:

- Mesajlar sayfasında "Önce sor" kutusunu kapatırsan SMS, Gönder'e basar basmaz gider. Bu tercih kalıcıdır.
- Rehberde bir kişiyi düzenleyip kaydetmek eski numara ve e-postaları doğrudan değiştirir.
- Terminal ve Geliştirici Araçları'na yazdığın komutlar olduğu gibi çalışır.
- Canlı ekrana bırakılan (APK olmayan) dosyalar doğrudan telefonun Download klasörüne gönderilir.
- "Kablosuz Bağlan", telefonda adb'yi ağ üzerinden dinlemeye açar (`adb tcpip 5555`); telefon yeniden
  başlayana ya da `adb usb` çalıştırılana kadar açık kalır.

## Bilinen sınırlar

- **Komut satırında görünen veriler:** Wi-Fi parolası, SMS metni ve kişi bilgileri telefona `adb shell`
  komutunun parçası olarak gider. Komut çalıştığı kısa süre boyunca bilgisayardaki süreç listesinde
  (Görev Yöneticisi, güvenlik yazılımları, komut satırı denetimi açıksa olay günlüğü) görünür.
  Paylaşılan ya da izlenen bilgisayarlarda bunu göz önünde bulundur.
- **Masaüstü uyarıları** (varsayılan olarak kapalı) açılırsa telefondaki bildirim metinleri, tek
  kullanımlık kodlar dahil, Windows bildirim merkezine düşer.
- **Kayıtlı terminal komutları** Windows kayıt defterinde düz metin olarak saklanır; içine parola yazma.
- **Canlı ekran bağlantısı** bilgisayarda `127.0.0.1` üzerindeki bir porta açılır. Aynı bilgisayarda
  çalışan başka bir program ya da kullanıcı bu porta bağlanmayı deneyebilir; ortak kullanılan
  bilgisayarlarda kullanma.
- **Dışa aktarılan CSV'ler:** `=`, `+`, `-`, `@` ile başlayan hücreler, tablo programları formül olarak
  çalıştırmasın diye başına `'` konarak yazılır.

## Güvenlik açığı bildirme

Bir güvenlik açığı bulduysan lütfen herkese açık bir issue açma. GitHub'da deponun **Security →
Report a vulnerability** bölümünden özel bildirim gönder. Bildirimde şunlar olsun:

- Etkilenen dosya ya da özellik
- Yeniden üretme adımları (mümkünse zararsız bir örnek girdi)
- Olası etki

Özellikle ilgilendiğimiz konular: telefondan ya da kullanıcıdan gelen metnin kabuk komutuna kaçması
(komut enjeksiyonu), telefondan gelen dosya adlarıyla bilgisayarda klasör dışına yazma, onaysız
çalışan geri alınamaz işlemler.

## Desteklenen sürümler

Yalnızca `main` dalındaki son sürüm için düzeltme yapılır.
