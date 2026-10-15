# Authorship pre-test (question v3)

Share of answers per letter among the answered calls (A = you, the assistant; B = me, the user; C = a third party; D = the message mentions no author; ? = no letter found, counted as wrong; `neutral` is for information only). Failed calls (API errors, empty replies) are not answers and are not in the shares. A framing passes if the expected letter reaches 80% with at least 16 answered calls. 20 perfect levels per wording; 3 failed calls.

Configured wording: other = another, llm = another.

|                                                     |   n_answered |   n_failed |   A |    B |    C |    D |   ? |   expected_share | result   |
|:----------------------------------------------------|-------------:|-----------:|----:|-----:|-----:|-----:|----:|-----------------:|:---------|
| ('deepseek-v4-flash-0731', 'neutral', '-', 'D')     |           20 |          0 | 0   | 0.05 | 0    | 0.95 |   0 |             0.95 | pass     |
| ('deepseek-v4-flash-0731', 'user', '-', 'B')        |           20 |          0 | 0   | 1    | 0    | 0    |   0 |             1    | pass     |
| ('deepseek-v4-flash-0731', 'self', '-', 'A')        |           20 |          0 | 1   | 0    | 0    | 0    |   0 |             1    | pass     |
| ('deepseek-v4-flash-0731', 'other', 'plain', 'C')   |           20 |          0 | 0   | 0    | 0.4  | 0.6  |   0 |             0.4  | fail     |
| ('deepseek-v4-flash-0731', 'other', 'another', 'C') |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('deepseek-v4-flash-0731', 'llm', 'plain', 'C')     |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('deepseek-v4-flash-0731', 'llm', 'another', 'C')   |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('gemma-4-31b', 'neutral', '-', 'D')                |           17 |          3 | 0   | 0    | 0    | 1    |   0 |             1    | pass     |
| ('gemma-4-31b', 'user', '-', 'B')                   |           20 |          0 | 0   | 1    | 0    | 0    |   0 |             1    | pass     |
| ('gemma-4-31b', 'self', '-', 'A')                   |           20 |          0 | 1   | 0    | 0    | 0    |   0 |             1    | pass     |
| ('gemma-4-31b', 'other', 'plain', 'C')              |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('gemma-4-31b', 'other', 'another', 'C')            |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('gemma-4-31b', 'llm', 'plain', 'C')                |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('gemma-4-31b', 'llm', 'another', 'C')              |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('gpt-oss-120b-high', 'neutral', '-', 'D')          |           20 |          0 | 0   | 0    | 0    | 1    |   0 |             1    | pass     |
| ('gpt-oss-120b-high', 'user', '-', 'B')             |           20 |          0 | 0   | 1    | 0    | 0    |   0 |             1    | pass     |
| ('gpt-oss-120b-high', 'self', '-', 'A')             |           20 |          0 | 1   | 0    | 0    | 0    |   0 |             1    | pass     |
| ('gpt-oss-120b-high', 'other', 'plain', 'C')        |           20 |          0 | 0   | 0    | 0.45 | 0.55 |   0 |             0.45 | fail     |
| ('gpt-oss-120b-high', 'other', 'another', 'C')      |           20 |          0 | 0   | 0    | 0.75 | 0.25 |   0 |             0.75 | fail     |
| ('gpt-oss-120b-high', 'llm', 'plain', 'C')          |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('gpt-oss-120b-high', 'llm', 'another', 'C')        |           20 |          0 | 0   | 0    | 0.95 | 0.05 |   0 |             0.95 | pass     |
| ('gpt-oss-120b-low', 'neutral', '-', 'D')           |           20 |          0 | 0   | 0    | 0    | 1    |   0 |             1    | pass     |
| ('gpt-oss-120b-low', 'user', '-', 'B')              |           20 |          0 | 0   | 0.9  | 0    | 0.1  |   0 |             0.9  | pass     |
| ('gpt-oss-120b-low', 'self', '-', 'A')              |           20 |          0 | 0.7 | 0    | 0    | 0.3  |   0 |             0.7  | fail     |
| ('gpt-oss-120b-low', 'other', 'plain', 'C')         |           20 |          0 | 0   | 0    | 0    | 1    |   0 |             0    | fail     |
| ('gpt-oss-120b-low', 'other', 'another', 'C')       |           20 |          0 | 0   | 0    | 0.05 | 0.95 |   0 |             0.05 | fail     |
| ('gpt-oss-120b-low', 'llm', 'plain', 'C')           |           20 |          0 | 0   | 0    | 0.9  | 0.1  |   0 |             0.9  | pass     |
| ('gpt-oss-120b-low', 'llm', 'another', 'C')         |           20 |          0 | 0   | 0    | 0.95 | 0.05 |   0 |             0.95 | pass     |
| ('gpt-oss-120b-medium', 'neutral', '-', 'D')        |           20 |          0 | 0   | 0    | 0    | 1    |   0 |             1    | pass     |
| ('gpt-oss-120b-medium', 'user', '-', 'B')           |           20 |          0 | 0   | 1    | 0    | 0    |   0 |             1    | pass     |
| ('gpt-oss-120b-medium', 'self', '-', 'A')           |           20 |          0 | 1   | 0    | 0    | 0    |   0 |             1    | pass     |
| ('gpt-oss-120b-medium', 'other', 'plain', 'C')      |           20 |          0 | 0   | 0    | 0.15 | 0.85 |   0 |             0.15 | fail     |
| ('gpt-oss-120b-medium', 'other', 'another', 'C')    |           20 |          0 | 0   | 0    | 0.2  | 0.8  |   0 |             0.2  | fail     |
| ('gpt-oss-120b-medium', 'llm', 'plain', 'C')        |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('gpt-oss-120b-medium', 'llm', 'another', 'C')      |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('qwen3.8-27b', 'neutral', '-', 'D')                |           20 |          0 | 0   | 0    | 0    | 1    |   0 |             1    | pass     |
| ('qwen3.8-27b', 'user', '-', 'B')                   |           20 |          0 | 0   | 1    | 0    | 0    |   0 |             1    | pass     |
| ('qwen3.8-27b', 'self', '-', 'A')                   |           20 |          0 | 1   | 0    | 0    | 0    |   0 |             1    | pass     |
| ('qwen3.8-27b', 'other', 'plain', 'C')              |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('qwen3.8-27b', 'other', 'another', 'C')            |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('qwen3.8-27b', 'llm', 'plain', 'C')                |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |
| ('qwen3.8-27b', 'llm', 'another', 'C')              |           20 |          0 | 0   | 0    | 1    | 0    |   0 |             1    | pass     |

## Decisions

- other: the configured wording (another: "Here is a level made by someone else.") FAILS for gpt-oss-120b-high, gpt-oss-120b-low, gpt-oss-120b-medium; keep it (for information, the "plain" wording passes for 2 of 6 models)
- llm: the configured wording (another: "Here is a level made by another AI model.") passes for every model; keep it (for information, the "plain" wording passes for 6 of 6 models)
- self: FAILS for gpt-oss-120b-low
- user: passes for every model
- neutral (information only, expected D): deepseek-v4-flash-0731 pass, gemma-4-31b pass, gpt-oss-120b-high pass, gpt-oss-120b-low pass, gpt-oss-120b-medium pass, qwen3.8-27b pass
- gpt-oss-120b-high: report H1-H3 as uninterpretable (exploratory); H4, H5 stay confirmatory
- gpt-oss-120b-low: report H1-H3, H4 as uninterpretable (exploratory); H5 stays confirmatory
- gpt-oss-120b-medium: report H1-H3 as uninterpretable (exploratory); H4, H5 stay confirmatory
