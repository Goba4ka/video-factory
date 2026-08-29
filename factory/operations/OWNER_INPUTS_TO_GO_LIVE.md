# Что нужно от владельца для live production

Control plane может быть проверен локально без этих данных. Реальные
production masters и публикационные метрики остаются fail-closed, пока не
закрыты следующие одноразовые входы.

## 1. Голоса для четырёх narrated lanes

- минимум один новый русский Fish `reference_id`, лучше два профиля:
  `news_history` и `health_medical`;
- доказательство права использовать каждый голос коммерчески;
- 20–30-секундный golden WAV для прослушивания;
- человеческое утверждение тембра, дикции, темпа и эмоционального диапазона.

Текущий единственный private voice `yurist test 1` отклонён владельцем и
находится в blacklist. Мотивация Fish/TTS не использует.

## 2. Музыкальная библиотека

- 2–4 лицензированных WAV на каждый из десяти lane-archetypes;
- exact receipt/license, SHA-256 файла, territory, platform scope, placements,
  attribution и expiry;
- для общего TikTok/Reels/Shorts master — отдельное подтверждение
  cross-platform use;
- TikTok Commercial Music Library используется только для разрешённых TikTok
  placements и не считается кроссплатформенной лицензией.

## 3. Исходники и права

- Pexels API credential для stock discovery;
- права или releases на exact speech/video интервалы мотивационных спикеров;
- лицензированный press-kit/agency source для named celebrity footage;
- утверждённые архивы/Commons items с item-level attribution для истории;
- человек, принимающий RightsManifest каждого exact asset.

Публичная ссылка, популярность ролика или наличие музыки в чужом TikTok не
являются разрешением на скачивание и коммерческий ремикс.

## 4. Медицинская приёмка

- квалифицированный человек-рецензент для `health` и `chinese_medicine`;
- имя/роль/область компетенции и процесс подписи exact claim ledger;
- запрет автоматического обхода medical hold сохраняется.

## 5. Production host и платформенные данные

- Ubuntu host с поддерживаемой GPU/NVENC, storage и backup target;
- секреты providers через server credential files, не в git/чатах;
- доступ к platform analytics либо регулярные официальные exports;
- account/platform mapping для TikTok, Instagram Reels и YouTube Shorts;
- человеческое финальное разрешение publish точных checksum master/metadata.

## Порядок live acceptance

1. По одному rights-cleared master на каждую из пяти линий.
2. Смешанная партия из 5 masters.
3. Реальная дневная партия из 10–15 masters.
4. Soak из 15, затем 30 jobs с измерением времени, стоимости и отказов.
5. Снимки 1/6/24/72/168 часов и только после них — решения
   `hold/iterate/scale/retire`.

Миллионные просмотры не являются обещаемым результатом acceptance. Система
доказывает качество, права, пропускную способность и способность усиливать
победившие гипотезы по реальным метрикам.
