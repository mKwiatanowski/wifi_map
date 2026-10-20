# ADR-0002: Instalacja natywna na Windows, bez Dockera

Status: przyjęty · 2026-10-19

## Kontekst
Docelowa maszyna: komputer z Windows 11 działający 24/7. Docker Desktop na Windows uruchamia kontenery w WSL2 za NAT.

## Decyzja
Aplikacja, AdGuard Home i Grafana są instalowane natywnie jako usługi Windows (`scripts/install.ps1`, WinSW dla aplikacji).

## Powody
- AdGuard w kontenerze widziałby wszystkie zapytania DNS z jednego IP (brama Dockera) — tracimy przypisanie DNS do urządzeń.
- Kontener nie widzi warstwy 2 LAN — skan ARP nie działa.
- Docker Desktop w większych organizacjach wymaga płatnej licencji.

## Konsekwencje
+ Pełna funkcjonalność, mniejsze zużycie zasobów.
− Skrypt instalacyjny specyficzny dla Windows. Na Linux/Raspberry Pi w przyszłości można dodać `docker-compose.yml` z `network_mode: host`.
