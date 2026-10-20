# ADR-0001: SQLite jako jedyne źródło prawdy + Grafana do analityki

Status: przyjęty · 2026-10-19

## Kontekst
Potrzebujemy logiki domenowej (osoby, wzorce, alerty) i bogatych wykresów. Skala: kilka osób, ~20 urządzeń, 30 dni.

## Decyzja
Własna aplikacja w Pythonie zapisuje wszystko do jednej bazy SQLite (WAL). Grafana czyta ją tylko do odczytu przez wtyczkę `frser-sqlite-datasource` i jest osadzona w panelu przez reverse proxy `/grafana`.

## Konsekwencje
+ Jedna baza, prosty backup (jeden plik), brak synchronizacji.
+ Grafana jest opcjonalna — aplikacja działa bez niej.
− Przy dużo większej skali trzeba by przejść na TimescaleDB/InfluxDB (warstwa `repositories` to izoluje).
