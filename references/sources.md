# Первичные источники и совместимость

Документация сверена 17 сентября 2026 года. Ни один из этих источников не является доказательством, что локальная сборка пользователя уже содержит нужные инструменты или что её API-подключение работает.

1. Hermes, Skills System — формат SKILL.md, локальный каталог пользовательских скиллов, поддерживающие scripts/references и объявление переменных окружения: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
2. Hermes, Subagent Delegation — отдельный контекст worker, базовые goal/context, существующий маршрут delegation.model, отсутствие per-task model в стандартном delegate_task: https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation
3. TypeSafe, HTTP API — endpoint, request state/model/questions, Choice answer и usage: https://docs.typesafe.ai/api
4. TypeSafe, Choice — варианты ответа, probabilities/confidence, независимые вопросы: https://docs.typesafe.ai/primitives/choice
5. TypeSafe, Models — опубликованный фиксированный ID jev-1.13.0; движущиеся aliases намеренно не используются: https://docs.typesafe.ai/models

Код пакета написан для данного скилла и не является копией исходников Hermes или TypeSafe SDK. HTTP-клиент реализует только нужный здесь поднабор Choice. Пакет не использует приватные Hermes API, middleware, plugin LLM access или собственный механизм доступа к подписочным провайдерам.

Ранее упомянутая пользователем сборка Hermes v0.21.1 сама по себе не подтверждает её текущий набор инструментов. Полноценный прогон должен выполняться на реальной установленной сборке. Старый read-only тестовый профиль нельзя молча считать конфигурацией основного Hermes.
