-- Painel de acompanhamento do rastreio.
--
--   ./deploy/monitor.sh
--
-- Roda em cima de `consulta_log`, a tabela de auditoria. Nenhuma consulta aqui
-- expoe email em claro: a coluna `email_hmac` so aparece em contagens de
-- DISTINCT, nunca em valor.
--
-- Todas as datas sao convertidas para America/Sao_Paulo. Sem isso, "hoje" no
-- relatorio comecaria as 21h do dia anterior -- o banco guarda em UTC.

\pset null '-'

\echo
\echo ==========================================================
\echo  1. PANORAMA DIARIO -- ultimos 14 dias
\echo ==========================================================

SELECT (criado_em AT TIME ZONE 'America/Sao_Paulo')::date  AS dia,
       count(*)                                            AS total,
       count(*) FILTER (WHERE resultado = 'sucesso')        AS sucesso,
       count(*) FILTER (WHERE resultado = 'sem_rastreio')   AS sem_rastreio,
       count(*) FILTER (WHERE resultado = 'vazio_fr')       AS vazio_fr,
       count(*) FILTER (WHERE resultado = 'nao_encontrado') AS nao_encontrado,
       count(*) FILTER (WHERE resultado = 'erro_externo')   AS erro_externo
FROM consulta_log
WHERE criado_em > now() - interval '14 days'
GROUP BY dia
ORDER BY dia DESC;

\echo
\echo ==========================================================
\echo  2. TAXA DE vazio_fr -- o sinal de bug sistemico
\echo ==========================================================
\echo  Denominador = pedidos JA DESPACHADOS (sucesso + vazio_fr).
\echo  Acima de 20 por cento, com ao menos 10 consultas no dia, investigar:
\echo  provavel divergencia de numero entre a Shopify e o ERP.

SELECT (criado_em AT TIME ZONE 'America/Sao_Paulo')::date AS dia,
       count(*) FILTER (WHERE resultado IN ('sucesso', 'vazio_fr')) AS despachados,
       count(*) FILTER (WHERE resultado = 'vazio_fr')               AS vazio_fr,
       round(
           100.0 * count(*) FILTER (WHERE resultado = 'vazio_fr')
           / nullif(count(*) FILTER (WHERE resultado IN ('sucesso', 'vazio_fr')), 0),
           1
       ) AS taxa_pct
FROM consulta_log
WHERE criado_em > now() - interval '14 days'
GROUP BY dia
HAVING count(*) FILTER (WHERE resultado IN ('sucesso', 'vazio_fr')) > 0
ORDER BY dia DESC;

\echo
\echo ==========================================================
\echo  3. FALHAS EXTERNAS -- por hora, ultimas 48h
\echo ==========================================================
\echo  Concentracao em poucas horas = instabilidade pontual da FR
\echo  ou da Shopify. Espalhado e continuo = problema nosso.

SELECT date_trunc('hour', criado_em AT TIME ZONE 'America/Sao_Paulo') AS hora,
       count(*) AS erros
FROM consulta_log
WHERE resultado = 'erro_externo'
  AND criado_em > now() - interval '48 hours'
GROUP BY hora
ORDER BY hora DESC;

\echo
\echo ==========================================================
\echo  4. POSSIVEL ENUMERACAO DE PEDIDOS
\echo ==========================================================
\echo  Assinatura de ataque: MUITOS pedidos distintos partindo do
\echo  mesmo IP. Cliente confuso repete o MESMO numero varias vezes
\echo  -- tentativas altas com pedidos_distintos baixo e normal.

SELECT ip_origem,
       count(*)                            AS tentativas,
       count(DISTINCT numero_pedido)       AS pedidos_distintos,
       count(DISTINCT email_hmac)          AS emails_distintos,
       max(criado_em AT TIME ZONE 'America/Sao_Paulo') AS ultima
FROM consulta_log
WHERE resultado = 'nao_encontrado'
  AND criado_em > now() - interval '7 days'
GROUP BY ip_origem
HAVING count(*) >= 5
ORDER BY pedidos_distintos DESC, tentativas DESC
LIMIT 20;

\echo
\echo ==========================================================
\echo  5. ANOMALIAS REGISTRADAS -- 14 dias
\echo ==========================================================
\echo  multiplos_fulfillments / multiplos_trackings: a premissa de
\echo  um pedido para um rastreio foi violada, e o contrato precisa
\echo  virar uma lista de remessas.

SELECT a AS anomalia, count(*) AS ocorrencias
FROM consulta_log, LATERAL jsonb_array_elements_text(anomalias::jsonb) AS a
WHERE criado_em > now() - interval '14 days'
GROUP BY a
ORDER BY ocorrencias DESC;

\echo
\echo ==========================================================
\echo  6. LATENCIA -- 7 dias
\echo ==========================================================
\echo  Desempenho apenas. NAO serve para auditar o vazamento por
\echo  tempo: pedido inexistente e email divergente sao gravados
\echo  com o MESMO resultado, entao nao da para separa-los aqui,
\echo  que e exatamente a propriedade que queremos.

SELECT resultado,
       count(*)                                                  AS consultas,
       round(avg(latencia_ms))                                   AS media_ms,
       percentile_disc(0.5)  WITHIN GROUP (ORDER BY latencia_ms)  AS p50_ms,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY latencia_ms)  AS p95_ms,
       max(latencia_ms)                                          AS max_ms
FROM consulta_log
WHERE criado_em > now() - interval '7 days'
  AND latencia_ms IS NOT NULL
GROUP BY resultado
ORDER BY consultas DESC;

\echo
\echo ==========================================================
\echo  7. CACHE E DISTRIBUICAO POR CNPJ -- 7 dias
\echo ==========================================================

SELECT coalesce(cnpj, '(sem dados)')            AS cnpj,
       count(*)                                 AS consultas,
       count(*) FILTER (WHERE veio_do_cache)    AS do_cache
FROM consulta_log
WHERE criado_em > now() - interval '7 days'
GROUP BY cnpj
ORDER BY consultas DESC;

\echo
\echo ==========================================================
\echo  8. RETENCAO LGPD -- o expurgo esta rodando?
\echo ==========================================================
\echo  A coluna dias acima de 90 significa que o expurgo parou.

SELECT count(*)                                                AS registros,
       min(criado_em AT TIME ZONE 'America/Sao_Paulo')          AS mais_antigo,
       now()::date - min(criado_em)::date                       AS dias
FROM consulta_log;
