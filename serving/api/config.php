<?php
declare(strict_types=1);

/*
 * 本番では database_path を、アップロードした stocks.sqlite の絶対パスに
 * 変更できます。DVC_DATABASE_PATH / DVC_DAILY_LIMIT はローカル検証用です。
 */
$dailyLimitFromEnvironment = getenv('DVC_DAILY_LIMIT');
$dailyLimit = $dailyLimitFromEnvironment !== false
    ? filter_var($dailyLimitFromEnvironment, FILTER_VALIDATE_INT)
    : 300;

return [
    'database_path' => getenv('DVC_DATABASE_PATH') ?: __DIR__ . '/../../data/stocks.sqlite',
    'daily_limit' => is_int($dailyLimit) && $dailyLimit > 0 ? $dailyLimit : 300,
];
