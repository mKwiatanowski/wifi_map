# ADR-0003: AdGuard Home jako serwer DNS i źródło danych o ruchu

Status: przyjęty · 2026-10-19

## Kontekst
Router TP-Link (np. seria Archer, obsługiwany przez `tplinkrouterc6u`) nie udostępnia logów DNS ani historii połączeń. Chcemy widzieć, z czym łączą się urządzenia, i móc blokować.

## Decyzja
AdGuard Home na komputerze docelowym; router rozdaje go przez DHCP jako DNS. Aplikacja czyta `/control/querylog` i steruje blokadami przez REST API. Wolumen transferu bierzemy z routera (`up_speed`/`down_speed` per urządzenie).

## Konsekwencje
+ Per-urządzenie DNS, gotowe listy blokujące, blokowanie z UI.
− Gdy komputer docelowy jest wyłączony, sieć traci DNS → README zaleca ustawienie zapasowego DNS w routerze (Secondary DNS), co jednak częściowo omija logowanie.
− Urządzenia z zakodowanym DNS (DoH, np. niektóre TV/Chrome) omijają AdGuard — ograniczenie znane, opisane w UI.
