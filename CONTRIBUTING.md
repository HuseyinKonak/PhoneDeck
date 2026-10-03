# Katkı

## Kurulum

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests -q
```

Testler sahte bir adb kullanır; telefon gerekmez.

## Kurallar

- **Kabuk komutları:** telefona giden her metin `adb.shell` ile telefonun kabuğunda çalışır. Kullanıcıdan
  ya da telefondan gelen her değer katı bir regex ile doğrulanmalı, `int()` ile dönüştürülmeli ya da
  `shlex.quote` ile tırnaklanmalı. Her yeni komut için enjeksiyon denemesi içeren bir test ekle
  (örnekler: `tests/test_finder.py` içindeki `INJECTIONS`).
- **Geri alınamayan işlemler** (gönderme, silme, sıfırlama) arayüzde onay sormalı.
- **Katmanlar:** `phonedeck/core` Qt'ye bağlı değildir ve testlidir; `phonedeck/ui` yalnızca arayüzdür.
  Telefonla konuşan mantığı `core` içine yaz.
- **Arayüz iş parçacığı:** telefon çağrıları `Page.run` / `Page.read` ile arka planda çalışır; arayüz
  donmamalı.
- **Testler:** yeni çekirdek kodu test ile gelir; `phonedeck.core` kapsamı %80'in altına düşmemeli.
- **Kişisel veri:** gerçek telefon numarası, kişi adı, mesaj, seri numarası ya da konum içeren test
  verisi, ekran görüntüsü ve dışa aktarım dosyası gönderme.

## Telefondaki yardımcıyı derleme

`tools/applister/*.java` değişirse `phonedeck/assets/applister.jar` yeniden derlenmeli:

```
powershell -ExecutionPolicy Bypass -File tools\applister\build.ps1
```

JDK ve Android SDK (platform + build-tools) gerekir.

## Commit biçimi

`<tür>: <açıklama>` — türler: feat, fix, refactor, docs, test, chore, perf, ci
