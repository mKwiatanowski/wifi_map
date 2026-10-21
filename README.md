# HomeSense

Domowy monitoring własnej sieci Wi-Fi na Windows 11. HomeSense sprawdza, które urządzenia są w sieci,
i na tej podstawie pokazuje, kto jest w domu, uczy się zwykłego rytmu dnia domowników, wykrywa
nietypowe sytuacje (np. „nikt nie wrócił o zwykłej porze”, nowe nieznane urządzenie) i wysyła
powiadomienia. Dzięki AdGuard Home widać też, z jakimi domenami łączą się poszczególne urządzenia,
i można blokować reklamy oraz wybrane serwisy.

Składniki na komputerze docelowym (Windows 11, działający 24/7):

| Składnik | Adres | Rola |
|---|---|---|
| HomeSense (usługa Windows) | `http://<IP-komputera>:8080` | panel web + PWA, kolektory, alerty |
| AdGuard Home | DNS `:53`, panel `http://127.0.0.1:3001` | serwer DNS sieci domowej, log zapytań |
| Grafana OSS | `127.0.0.1:3000` (tylko lokalnie) | wykresy, osadzone w panelu pod `/grafana/` |

Źródła danych: router TP-Link obsługiwany przez bibliotekę `tplinkrouterc6u`, np. seria Archer (lista klientów i transfer), skan ARP sieci lokalnej,
log zapytań AdGuard Home.

## Prywatność

**Informacja dla domowników — przeczytajcie przed instalacją.**

- HomeSense zbiera wyłącznie **metadane**: które urządzenie (adres MAC/IP) jest w sieci i kiedy, ile
  danych przesyła oraz nazwy domen, o które pyta (np. `youtube.com`). **Nie** widzi treści stron,
  wiadomości, haseł ani tego, co jest oglądane — nie ma przechwytywania ruchu ani łamania szyfrowania.
- Na tej podstawie wyliczana jest obecność w domu i typowe godziny wyjść i powrotów. To dane
  wrażliwe — każdy domownik powinien wiedzieć, że system działa, i zgodzić się na to.
- Każda osoba może mieć tryb **„Tylko statystyki”** (`privacy_mode = stats_only`): wtedy dla jej
  urządzeń nazwy domen w ogóle nie trafiają do bazy, zapisywane są tylko liczby zapytań.
- Dane są przechowywane tylko lokalnie na komputerze docelowym (`C:\HomeSense\data`) i automatycznie usuwane po
  **30 dniach** (`HOMESENSE_RETENTION_DAYS`). Wyjątek: plan mieszkania i punkty pomiaru zasięgu
  Wi-Fi (strona **Zasięg Wi-Fi**) to dane wprowadzone przez Was — nie są usuwane automatycznie, tylko
  wtedy, gdy je usuniecie w panelu. Nic nie jest wysyłane do chmury poza powiadomieniami,
  które sami włączycie (Telegram/ntfy).
- Strona **Prywatność** w panelu pokazuje, co jest zbierane, i pozwala zmienić tryb dla każdej osoby.
- Panel wymaga zalogowania. Konta są w osobnej bazie `auth.db` (w podkatalogu `data\auth\`, razem
  z jednorazowym tokenem konfiguracji); dane monitoringu w `homesense.db`. Grafana czyta wyłącznie
  `homesense.db`, i to tylko do odczytu — do katalogu `data\auth\` jej konto nie ma żadnego dostępu.

## Wymagania

- Windows 11 (Windows PowerShell 5.1 — jest w systemie), konto administratora.
- Python 3.12 lub nowszy **zainstalowany dla wszystkich użytkowników** (np. w `C:\Program Files`).
  Instalacja „tylko dla mnie” (w profilu użytkownika) jest pomijana, bo konto usługi nie ma do niej
  dostępu; wtedy instalator sam doinstaluje 3.12 przez `winget` (`--scope machine`).
- Komputer docelowy podłączony do sieci domowej (najlepiej kablem), profil sieci **Prywatna**.
- Router TP-Link obsługiwany przez bibliotekę `tplinkrouterc6u` (np. seria Archer) z hasłem do panelu
  administracyjnego. Ustaw w `.env` swój adres routera (`HOMESENSE_ROUTER_HOST`, domyślnie fabryczny
  `192.168.0.1`) i podsieć do skanu ARP (`HOMESENSE_ARP_SUBNET`, domyślnie `192.168.0.0/24`).
- Dostęp do internetu podczas instalacji (pobierane są WinSW, AdGuard Home, Grafana i pakiety Pythona).
  Pobierane pliki mają **przypięte wersje i sumy SHA-256** zapisane w `install.ps1` (WinSW v2.12.0,
  AdGuard Home v0.107.79, Grafana 12.2.0 jako zapasowe archiwum zip); przy niezgodności sumy plik
  jest usuwany, a instalacja przerywana. Pakiety z `winget` (Python, Grafana) weryfikuje sam winget
  na podstawie sum z podpisanego manifestu. Nowszą wersję wprowadza się, zmieniając adres i sumę
  w skrypcie.

## Instalacja

Wszystkie kroki wykonuje się na komputerze docelowym.

1. **Pobierz repozytorium** (np. `git clone` lub rozpakuj ZIP) do dowolnego katalogu, np.
   `C:\src\homesense`.
2. **Uruchom PowerShell jako administrator** (menu Start → „Windows PowerShell” → „Uruchom jako
   administrator”) i wykonaj:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   cd C:\src\homesense
   .\scripts\install.ps1                      # domyślnie instaluje do C:\HomeSense
   # opcje: -InstallDir D:\HomeSense  -SkipAdGuard  -SkipGrafana  -AdminUser ania
   ```

3. **Plik `.env`** — instalator kopiuje `.env.example` do `C:\HomeSense\.env` i otwiera go w Notatniku.
   Wpisz `HOMESENSE_ROUTER_PASSWORD` (hasło do panelu routera). Możesz od razu wpisać
   `HOMESENSE_ADGUARD_USERNAME` / `HOMESENSE_ADGUARD_PASSWORD` — te same, które ustawisz za chwilę
   w kreatorze AdGuard. Zapisz i zamknij Notatnik, skrypt pójdzie dalej.
   - **Klucz sesji `HOMESENSE_SECRET_KEY` generuje się automatycznie** (48 losowych bajtów), jeśli
     jest pusty — nie trzeba go wymyślać, skrypt nigdy go nie wypisuje.
   - Uprawnienia: `C:\HomeSense` jest zablokowany od razu po utworzeniu (zanim powstanie venv czy
     usługa) — pełny dostęp mają tylko SYSTEM i Administratorzy, użytkownicy (`Users`) tylko odczyt
     i uruchamianie (potrzebne ikonie w zasobniku). Katalog danych (`C:\HomeSense\data`), logi
     i plik `.env` są dostępne wyłącznie dla SYSTEM, Administratorów, instalującego użytkownika
     i — w zakresie opisanym w punkcie 5 — konta usługi. Zmienna `HOMESENSE_DATA_DIR` jest
     ustawiana systemowo.
   - Konta (`auth.db`) i token konfiguracji leżą w `C:\HomeSense\data\auth\`. Ten katalog ma
     własne, niedziedziczone uprawnienia: pełny dostęp **tylko** SYSTEM, Administratorzy i konto
     usługi `NT SERVICE\HomeSense` — bez instalującego użytkownika, zwykłych użytkowników i Grafany — oraz
     dziedziczony zakaz dla konta Grafany (`LocalService`), obejmujący też pliki tworzone później
     (`auth.db-wal`, `auth.db-shm`, `setup-token.txt`). Uprawnienia są nakładane przy każdym
     uruchomieniu instalatora. Starsza instalacja z `data\auth.db` jest przenoszona automatycznie.
     Polecenia, które otwierają bazę kont (`homesense create-user`, `homesense init-db`,
     `homesense doctor`), uruchamiaj w PowerShellu **jako administrator**.
   - Plik `.env` zostaje w `C:\HomeSense` (nigdy w katalogu danych).
4. **Konto do panelu** — skrypt uruchamia `homesense create-user admin` i prosi o hasło (zalecane co najmniej 8
   znaków, dwa razy). Instalator działa jako administrator, a uprawnienia `data\auth\` są ustawiane
   przed tym krokiem, więc konto da się zapisać. Przy ponownej instalacji ten krok jest pomijany,
   jeśli konto już istnieje.
5. **Usługa HomeSense** — rejestrowana przez WinSW (`C:\HomeSense\HomeSense.exe`), start automatyczny,
   restart po awarii, logi z rotacją w `C:\HomeSense\logs`.
   - Usługa działa na **wirtualnym koncie `NT SERVICE\HomeSense`**, a nie jako LocalSystem — nie ma
     uprawnień administratora (wystarczają jej `ping`, `arp -a`, HTTP i port 8080). Konto dostaje
     tylko: odczyt i uruchamianie `C:\HomeSense` (venv), modyfikację katalogu danych i logów, pełny
     dostęp do `data\auth\` oraz odczyt `.env`. To nie jest konto Grafany (`LocalService`), które ma
     zakaz dostępu do `data\auth\`.
   - Ścieżki programów usług są zawsze w cudzysłowie (`<executable>` w XML WinSW oraz `binPath`
     ustawiany przez `sc.exe config`), więc nie ma problemu „niecytowanej ścieżki usługi”.
     Instalator sprawdza konto i ścieżkę po rejestracji i przerywa, jeśli się nie zgadzają.
   - Usługa nie ma pulpitu, więc **dymki Windows z usługi się nie pokażą** — instalator ustawia
     w nowym `.env` `HOMESENSE_WINDOWS_TOAST=false`. Powiadomienia zobaczysz w panelu (ikona
     w zasobniku, przeglądarka/PWA) oraz przez Telegram lub ntfy.
6. **AdGuard Home i kreator** — skrypt instaluje AdGuard Home jako usługę i otwiera w przeglądarce
   kreator pierwszego uruchomienia `http://127.0.0.1:3000`. W kreatorze ustaw:
   - **Interfejs administratora (Admin Web Interface): `127.0.0.1`, port `3001`** — tylko lokalnie
     (HomeSense łączy się z nim na `127.0.0.1`); port 3000 jest zarezerwowany dla Grafany,
   - **Serwer DNS: „Wszystkie interfejsy”, port `53`**,
   - login i hasło administratora (te same co w `.env`).

   Skrypt sam czeka (bez naciskania klawiszy), aż `http://127.0.0.1:3001/control/status` zacznie
   odpowiadać — maksymalnie 15 minut. Dopiero potem instaluje Grafanę. Jeśli czas minie, dokończ
   kreator i uruchom `.\scripts\install.ps1 -SkipAdGuard`.
7. **Grafana** — instalowana przez `winget install GrafanaLabs.Grafana.OSS` (w razie problemów
   z archiwum zip), z wtyczką `frser-sqlite-datasource`, konfiguracją `conf\custom.ini` i gotowymi
   dashboardami. Grafana nasłuchuje tylko na `127.0.0.1:3000`, działa na koncie
   `NT AUTHORITY\LocalService` (bez dostępu do `data\auth\`). Oglądasz ją w panelu HomeSense
   (Analityka), a nie bezpośrednio. Błąd instalacji Grafany nie przerywa reszty instalacji — skrypt
   ostrzega i kończy pozostałe kroki.
8. **Zapora** — reguły tylko dla profilu **Prywatny**: TCP 8080 (panel) oraz UDP/TCP 53 (DNS).
   Grafana (3000) i panel AdGuard (3001) nie dostają reguł — są dostępne tylko z komputera docelowego.
9. **Ikona w zasobniku** — skrót w autostarcie (`shell:startup`) uruchamia
   `venv\Scripts\pythonw.exe -m homesense.cli tray` (`homesense tray` bez okna konsoli).

### Ustawienia routera (TP-Link)

Bez tego AdGuard nie zobaczy zapytań DNS z innych urządzeń. Nazwy menu poniżej pochodzą z panelu
routerów TP-Link serii Archer; w innych modelach mogą się nieco różnić.

1. Zaloguj się do panelu routera (ten sam adres co `HOMESENSE_ROUTER_HOST` w `.env`; fabrycznie
   zwykle `http://192.168.0.1`).
2. **Advanced → Network → DHCP Server** (Zaawansowane → Sieć → Serwer DHCP):
   - **Primary DNS** = adres IP komputera docelowego (wypisany na końcu instalacji, np. `192.168.0.20`),
   - **Secondary DNS** — zostaw puste albo też IP komputera docelowego. Wpisanie np. `8.8.8.8` zapewni internet,
     gdy komputer docelowy jest wyłączony, ale część zapytań ominie AdGuard (niepełne statystyki).
   - Na ekranie widać tabelę „Address Reservation” pod ustawieniami puli adresów — tam w kolejnym
     kroku dodajesz rezerwację.
3. **Address Reservation** → *Add*: MAC karty sieciowej komputera docelowego → jego obecny adres IP. Komputer docelowy musi
   mieć zawsze ten sam adres, bo jest serwerem DNS.
4. Zapisz. Urządzenia dostaną nowy DNS przy odnowieniu dzierżawy DHCP (najszybciej: wyłącz i włącz
   Wi-Fi na telefonie).

### Telefony: stały adres MAC

iPhone i Android domyślnie używają losowego (prywatnego) adresu MAC, który może się zmieniać.
W ustawieniach Wi-Fi **tej** sieci wybierz **„stały prywatny adres”**:

- iPhone: Ustawienia → Wi-Fi → (i) przy sieci → *Prywatny adres Wi-Fi* → **Stały**,
- Android: Wi-Fi → sieć → *Prywatność* / *Typ adresu MAC* → **Użyj adresu MAC urządzenia** lub stały
  losowy adres (zależnie od producenta).

### Pierwsze uruchomienie bez konta (token konfiguracji)

Normalnie konto zakłada instalator (`homesense create-user`), więc nie ma okna rejestracji w sieci.
Jeśli jednak usługa wystartuje, gdy nie istnieje żadne konto, zapisuje jednorazowy **token
konfiguracji** do pliku `C:\HomeSense\data\auth\setup-token.txt` (ważny **1 godzinę**, nigdy nie
trafia do logów). Wtedy:

1. Na komputerze docelowym, w PowerShellu uruchomionym **jako administrator** (katalog `data\auth\` jest
   dostępny tylko dla SYSTEM i Administratorów), wyświetl token:
   `Get-Content C:\HomeSense\data\auth\setup-token.txt`.
2. Otwórz `http://127.0.0.1:8080/setup`, wpisz nazwę konta, hasło i **wklej token do pola formularza**.
   Tokenu nigdy nie dopisuj do adresu strony.
3. Po wygaśnięciu tokenu: `Restart-Service HomeSense` wygeneruje nowy.

Alternatywnie w PowerShell (administrator): `cd C:\HomeSense; .\venv\Scripts\homesense.exe create-user admin`.

## Pierwsza konfiguracja

1. Zaloguj się do panelu `http://<IP-komputera>:8080` (na komputerze docelowym: `http://127.0.0.1:8080`).
2. **Osoby** → dodaj domowników (imię, kolor, tryb prywatności).
3. **Urządzenia** → przy każdym telefonie wybierz osobę i zaznacz **„Liczy się do obecności”**
   (`counts_for_presence`). Zaznaczaj tylko urządzenia noszone przy sobie (telefon, zegarek) —
   laptop czy telewizor zostają w domu i fałszowałyby obecność.
4. Obecność i wzorce budują się same; po kilku dniach pojawiają się profile tygodnia i alerty
   o nietypowych sytuacjach.
5. `homesense doctor` (w `C:\HomeSense`, PowerShell jako administrator) sprawdza połączenie z bazą, routerem, AdGuard i Grafaną.

Po każdej zmianie `.env`: `Restart-Service HomeSense`.

## Włączanie i wyłączanie monitoringu

W pasku u góry każdej strony panelu widać stan: **„Monitoring: włączony”** albo **„Monitoring:
wstrzymany do HH:MM”**. Przycisk **Wstrzymaj** pozwala wybrać czas: 1 h, 8 h, 24 h albo „do
odwołania”; **Wznów** włącza monitoring od razu. Te same przyciski są w **Ustawienia → Monitoring**.
Usługa cały czas działa, więc monitoring można włączyć z powrotem z panelu (bez `Stop-Service`).

Podczas wstrzymania HomeSense nie odpytuje routera, nie skanuje ARP, nie pobiera DNS z AdGuarda (także
w trybie symulacji), nie uruchamia detektorów anomalii i nie wysyła powiadomień. Zapytania DNS
zarejestrowane przez AdGuarda w czasie przerwy nie są później wczytywane. Stan obecności jest
„zamrożony”: przerwa nie oznacza, że wszyscy wyszli. Po wznowieniu HomeSense przez czas
`HOMESENSE_DEPARTURE_TIMEOUT_MIN` (domyślnie 15 min) nie zapisuje wyjść — kto jest w domu, zostanie w tym
czasie zauważony ponownie; kto wyszedł w czasie przerwy, dostanie wyjście z godziną ostatniego
zauważenia. Pauza na czas kończy się sama, a stan pauzy przetrwa restart usługi. Ikona w zasobniku
jest wtedy szara z podpisem „HomeSense — monitoring wstrzymany”.

## Mapa urządzeń

Zakładka **Mapa** pokazuje widok z góry: router w środku, wokół niego urządzenia w szacowanej odległości (pierścienie ~2 / 5 / 10 / 20 m, skala pierwiastkowa). Kolor kropki to kolor osoby (szary — nieprzypisane), litera to typ urządzenia (telefon, komputer, tablet, TV, IoT, nieznane). Można filtrować po typie i osobie, pokazać tylko urządzenia online i ślad z ostatnich 30 minut. Widok odświeża się co 10 s.

**Ograniczenie:** jeden router mierzy tylko siłę sygnału (RSSI) każdego urządzenia, nie kierunek. Odległość liczona jest modelem tłumienia `d = 10^((P0 − RSSI) / (10·n))` (domyślnie `HOMESENSE_RADAR_P0_DBM=-40`, `HOMESENSE_RADAR_PATH_LOSS_N=3.0`) z wygładzonego RSSI, więc ściany i trzymanie telefonu w dłoni zmieniają ją o kilka metrów. Kierunek na mapie jest umowny (stały dla urządzenia, wyliczany z adresu MAC). Urządzenia przewodowe stoją przy routerze, a te bez danych o sygnale są wypisane pod mapą.

## Zasięg Wi-Fi (mapa pokrycia)

Zakładka **Zasięg Wi-Fi** pozwala zmierzyć, gdzie w mieszkaniu sygnał jest dobry, a gdzie słaby:

1. **Plan** — podaj szerokość i długość mieszkania w metrach i kliknij „Zapisz wymiary”. Wybierz
   „rysuje pokój”, wpisz nazwę i przeciągnij prostokąt na planie (siatka co 1 m, przyciąganie co
   25 cm). Wybierz „ustawia router” i dotknij miejsca, gdzie stoi router. Pokoje usuwa się z listy pod
   planem. Plan zapisuje się od razu.
2. **Pomiar** — najlepiej z telefonu (PWA): wybierz swój telefon z listy urządzeń, zaznacz „mierzy
   sygnał”, stań w danym miejscu i dotknij go na planie. Przez wybrany czas (domyślnie 60 s)
   HomeSense zbiera RSSI, które router raportuje dla tego urządzenia; panel pokazuje odliczanie
   i wynik — medianę próbek. Punkt bez żadnej próbki jest oznaczony „brak próbek” i pomijany. Router
   podaje sygnał mniej więcej co minutę, więc nie ruszaj się do końca pomiaru. Pasmo (2,4 / 5 / 6 GHz)
   jest zapisywane, jeśli router je podaje.
3. **Mapa** — kolorowa mapa (zielony ≈ −30 dBm, czerwony ≈ −85 dBm) jest interpolowana między
   punktami (IDW). Można filtrować po paśmie i ukryć punkty. Pojedyncze punkty usuwa się z tabeli,
   wszystkie — przyciskiem „Wyczyść pomiary” (plan zostaje).

**Sygnał w czasie** (druga karta tej strony) liczy się sam, bez chodzenia po domu: tabela
urządzenie × godzina doby pokazuje średni sygnał (RSSI) z ostatnich 7 dni (czas lokalny), w tych
samych kolorach co mapa. Puste pole oznacza brak danych w tej godzinie; urządzenia przewodowe się nie
pojawiają. Można filtrować po typie urządzenia, tak jak na Mapie. Dane pochodzą z obserwacji routera,
więc podlegają zwykłej retencji (30 dni). To samo zestawienie jest w Grafanie: **Analityka →
Urządzenia → „Sygnał Wi-Fi wg godziny doby”**.

Pomiar nie działa, gdy monitoring jest wstrzymany. W trybie symulacji (dev) wybrane urządzenie
symulowanego domu dostaje sygnał wyliczony modelem tłumienia z odległości między routerem
a dotkniętym punktem (z niewielkim szumem), więc mapę można przetestować bez prawdziwej sieci.

## Powiadomienia

Kanały konfiguruje się w `C:\HomeSense\.env`, a to, które zdarzenia trafiają na który kanał (oraz
ciszę nocną) — w panelu: **Ustawienia → Powiadomienia**. Tam też jest przycisk „Wyślij test”.

- **Windows** — dymki systemowe (`HOMESENSE_WINDOWS_TOAST`) działają tylko przy uruchomieniu
  z sesji użytkownika (np. `dev.ps1`). Usługa HomeSense działa na koncie usługi bez pulpitu, więc
  na komputerze docelowym instalator wyłącza je w `.env`; używaj ikony w zasobniku, panelu/PWA, Telegrama lub ntfy.
- **Telegram**:
  1. W Telegramie napisz do **@BotFather** → `/newbot` → podaj nazwę → dostaniesz token bota.
  2. Napisz dowolną wiadomość do swojego nowego bota.
  3. Otwórz w przeglądarce `https://api.telegram.org/bot<TOKEN>/getUpdates` i odczytaj `chat.id`.
  4. W `.env`: `HOMESENSE_TELEGRAM_BOT_TOKEN=<TOKEN>`, `HOMESENSE_TELEGRAM_CHAT_ID=<chat.id>`.
- **ntfy** (bez konta):
  1. Wymyśl trudny do odgadnięcia temat, np. `homesense-7f3k9q2x` (temat działa jak hasło).
  2. W aplikacji ntfy (Android/iOS) subskrybuj ten temat na serwerze `https://ntfy.sh`.
  3. W `.env`: `HOMESENSE_NTFY_TOPIC=homesense-7f3k9q2x` (opcjonalnie własny `HOMESENSE_NTFY_URL`).

## Ikona w zasobniku i aplikacja na telefonie (PWA)

- **Komputer docelowy**: po zalogowaniu do Windows w zasobniku pojawia się ikona HomeSense (kolor = stan
  systemu; szara — monitoring wstrzymany). Kliknięcie otwiera okno panelu; menu: „Otwórz
  w przeglądarce”, „Wstrzymaj / wznów monitoring” i „Wstrzymaj router 15 min” (oba otwierają
  Ustawienia — zmiana wymaga zalogowania), „Zakończ”.
- **Telefon**: otwórz `http://<IP-komputera>:8080` w przeglądarce będąc w domowej sieci, zaloguj się
  i wybierz **„Dodaj do ekranu głównego”** (Safari: przycisk Udostępnij; Chrome: menu ⋮). Panel
  działa wtedy jak aplikacja. Poza domem nie jest dostępny (to celowe — nic nie jest wystawione do
  internetu).

## Rozwiązywanie problemów

- **Wylogowuje mnie z panelu routera** — routery TP-Link (np. seria Archer) pozwalają na jedną sesję administratora naraz.
  HomeSense loguje się co `HOMESENSE_ROUTER_INTERVAL_S` sekund i „wyrzuca” sesję w przeglądarce
  (i odwrotnie: gdy jesteś zalogowany, kolektor routera chwilowo zgłasza błąd). Na czas zmian
  w routerze kliknij **Ustawienia → Wstrzymaj router na 15 min** w panelu (albo „Wstrzymaj router
  15 min” w menu ikony w zasobniku), zatrzymaj usługę (`Stop-Service HomeSense`) lub zwiększ interwał
  w `.env`.
- **Brak zapytań DNS z urządzenia / puste statystyki domen** — urządzenie omija AdGuard:
  - przeglądarka używa DNS-over-HTTPS (Chrome: Ustawienia → Prywatność → *Używaj bezpiecznego DNS*
    — wyłącz; Firefox: *DNS przez HTTPS* — wyłącz; iPhone: *Przekaźnik prywatny iCloud*),
  - urządzenie ma ręcznie ustawiony DNS (np. `8.8.8.8`) albo router rozdaje Secondary DNS,
  - urządzenie nie odnowiło dzierżawy DHCP — wyłącz i włącz Wi-Fi.
  Takie zapytania są niewidoczne; to znane ograniczenie (bez MITM).
- **Telefon pojawia się jako nowe, nieznane urządzenie / obecność „skacze”** — losowy adres MAC.
  Ustaw „stały prywatny adres” (patrz wyżej) i przypisz nowy wpis w **Urządzeniach** do osoby
  (z zaznaczonym „Liczy się do obecności”); w starym wpisie odznacz „Liczy się do obecności”.
- **Telefon na chwilę znika z Wi-Fi** — to normalne (uśpienie). Wyjście jest odnotowywane dopiero
  po `HOMESENSE_DEPARTURE_TIMEOUT_MIN` minutach (domyślnie 15).
- **Usługa nie startuje** — zobacz `C:\HomeSense\logs\HomeSense.err.log`. Najczęściej: brak lub
  błędny `.env`, zajęty port 8080. W trybie `live` usługa odmawia startu bez poprawnego
  `HOMESENSE_SECRET_KEY` — uruchom ponownie `install.ps1` (wygeneruje klucz). Błąd „Access is
  denied”/„Odmowa dostępu” oznacza brak uprawnień konta `NT SERVICE\HomeSense` (np. Python
  zainstalowany tylko dla jednego użytkownika albo ręcznie zmienione uprawnienia) — ponowne
  uruchomienie `install.ps1` nakłada je od nowa. Nie przełączaj usługi na LocalSystem.
- **Instalator przerywa: „Niezgodna suma SHA-256”** — pobrany plik różni się od przypiętej wersji
  (uszkodzone pobieranie lub podmieniony plik). Plik jest usuwany; spróbuj ponownie później. Nie
  wyłączaj sprawdzania sumy.
- **Panel niedostępny z telefonu** — sprawdź, czy sieć na komputerze docelowym ma profil *Prywatna*
  (`Get-NetConnectionProfile`; zmiana: `Set-NetConnectionProfile -InterfaceIndex <n> -NetworkCategory Private`).
- **Kreator AdGuard nie otwiera się / port 3000 zajęty** — jeśli Grafana jest już zainstalowana,
  zatrzymaj ją (`Stop-Service Grafana`), dokończ kreator na `http://127.0.0.1:3000` (port panelu
  AdGuard ustaw na 3001), potem `Start-Service Grafana`.
- **Grafana nie startuje jako LocalService** — instalator przestawia usługę Grafana na konto
  `NT AUTHORITY\LocalService` i nadaje mu prawo *Modyfikacja* do katalogu danych Grafany
  (`<Grafana>\data`) i katalogu danych HomeSense (SQLite w trybie WAL potrzebuje zapisu w katalogu
  bazy nawet przy odczycie), z dziedziczonym zakazem dostępu do `data\auth\` (konta). Konto usługi
  ustawiane jest poleceniem `sc.exe config Grafana obj= "NT AUTHORITY\LocalService" password= ""`
  (skrypt przekazuje je przez `Start-Process` jednym ciągiem argumentów, bo Windows PowerShell 5.1
  gubi pusty argument `""`). Jeśli Grafana nie wstaje:
  1. Sprawdź log: `C:\HomeSense\data\grafana\logs\grafana.log` i Podgląd zdarzeń (Dziennik aplikacji).
  2. Ponów nadanie uprawnień, uruchamiając ponownie `install.ps1 -SkipAdGuard`.
  3. Ostateczność: `sc.exe config Grafana obj= LocalSystem` i `Start-Service Grafana`. Grafana
     działa wtedy z pełnymi uprawnieniami systemu (mogłaby odczytać `auth.db`) — traktuj to jako
     tymczasowe obejście i wróć do LocalService po usunięciu przyczyny.
- **Wykresy w „Analityce” są puste** — `homesense doctor` (Grafana `OK`?), sprawdź w panelu, czy
  są dane; dashboardy odświeżają się co minutę.
- **Odinstalowanie** — `.\scripts\uninstall.ps1` (PowerShell jako administrator). Bez dodatkowych
  parametrów dane zostają, łącznie z kontami w `data\auth\` i plikiem `.env`. `-RemoveAdGuard` usuwa też
  AdGuard (najpierw przywróć DNS w routerze!), `-RemoveData` kasuje cały katalog danych (z `data\auth\`),
  `.env`, logi i venv.

## Rozwój

Na komputerze deweloperskim pracuj **wyłącznie w trybie symulowanym** — symulowany dom
z kilkoma osobami, bez kontaktu z routerem i siecią:

```powershell
.\scripts\dev.ps1
```

Skrypt tworzy `.venv`, instaluje `pip install -e ".[dev,desktop]"`, ustawia
`HOMESENSE_MODE=simulated` i `HOMESENSE_DATA_DIR=.\data-dev`, przy pierwszym uruchomieniu generuje
8 dni historii (`homesense simulate --days 8`) i startuje panel na `http://127.0.0.1:8080`
(pierwsze konto: `homesense create-user admin` albo token z `data-dev\auth\setup-token.txt`).
Skrypt wymaga Pythona 3.12 lub nowszego w `PATH` i przerywa z czytelnym komunikatem, gdy go brak.
To najprostszy sposób, żeby wypróbować HomeSense bez instalacji.

Testy i lint (bez dostępu do sieci):

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\ruff check .
```

## Rozbudowa

Mapa kodu, konwencje i przepisy na nowy kolektor (np. ESP32 CSI, mmWave), detektor anomalii, kanał
powiadomień, stronę UI czy dashboard Grafany są w [CLAUDE.md](CLAUDE.md). Projekt:
`docs/superpowers/specs/2026-10-19-homesense-design.md`, decyzje: `docs/adr/`.
