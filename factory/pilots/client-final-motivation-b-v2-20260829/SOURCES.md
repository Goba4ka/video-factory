# SOURCES

## Речь и видео

- Первичный источник: интервью Николая Цискаридзе у Надежды Стрелец —
  <https://www.youtube.com/watch?v=kCJjNwW_v4s>.
- Использованный фрагмент первичного файла:
  `factory/research/russian-motivation-1/tsiskaridze_strelets_20260614_full.mp4`,
  таймкод 01:23:02.679–01:23:33.870.
- `assets/video/speaker-v2-cfr.mp4` — локальная 1080×1920 H.264 CFR30 производственная
  прокси-копия. Все crop/scale, muted monochrome grade и монтажные punches запечены в файл;
  оригинальная речь вынесена в `assets/audio/speech.m4a`.
- Права: текущая сборка предназначена только для внутреннего клиентского предпросмотра.
  Для публичного или коммерческого размещения требуется разрешение правообладателя интервью.

## Музыка

- `Complicated` — Arulo, Mixkit Stock Music Free License:
  <https://mixkit.co/free-stock-music/trap/>.
- Прямой файл, опубликованный в machine-readable metadata Mixkit:
  <https://assets.mixkit.co/music/281/281.mp3>.
- Локальный монтажный фрагмент: `assets/audio/dark-tension-bed-mix.m4a`.
- Выбранный профиль: serious / tension / melodic trap. Исходник нормализован до -17 LUFS;
  под речью уровень снижен примерно до -20 LUFS и возвращается к -17 LUFS в паузах.
  Carve 0.30 запечён в asset как три фиксированных voice-space EQ notch, поэтому композиция
  не содержит тяжёлых realtime automation/envelope lanes.
- Финальный program mix `assets/audio/final-mix.m4a` объединяет речь и эту подложку:
  измерено -14.1 LUFS integrated, true peak -0.9 dBFS. В композиции нет дополнительного
  runtime duck/carve — двойная обработка исключена.

## Шрифт

- Oswald Variable, локальная копия `assets/fonts/Oswald-Variable.ttf`.

- Полный машиночитаемый аудит прав и SHA-256: `RIGHTS_LEDGER.json`.
