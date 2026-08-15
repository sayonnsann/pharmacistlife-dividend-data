<?php
declare(strict_types=1);

ini_set('display_errors', '0');
ini_set('log_errors', '1');
header('Content-Type: application/json; charset=utf-8');

function respond(mixed $payload, int $status = 200, ?string $cacheControl = null): never
{
    http_response_code($status);
    header('Cache-Control: ' . ($cacheControl ?? 'no-store'));
    $json = json_encode(
        $payload,
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE
    );
    if ($json === false) {
        http_response_code(500);
        echo '{"error":"応答データを作成できませんでした"}';
        exit;
    }
    echo $json;
    exit;
}

function errorResponse(string $message, int $status): never
{
    respond(['error' => $message], $status);
}

function getString(string $name, string $default = ''): string
{
    $value = $_GET[$name] ?? $default;
    if (!is_string($value)) {
        errorResponse('パラメータの形式が不正です', 400);
    }
    return trim($value);
}

function unicodeLength(string $value): int
{
    if (function_exists('mb_strlen')) {
        return mb_strlen($value, 'UTF-8');
    }
    $result = preg_match_all('/./us', $value, $matches);
    return $result === false ? 0 : $result;
}

function escapedLike(string $value): string
{
    return str_replace(['\\', '%', '_'], ['\\\\', '\\%', '\\_'], $value);
}

function numericParameter(string $name): ?float
{
    $raw = getString($name);
    if ($raw === '') {
        return null;
    }
    if (!is_numeric($raw)) {
        errorResponse($name . ' は数値で指定してください', 400);
    }
    $value = (float) $raw;
    if (!is_finite($value)) {
        errorResponse($name . ' は有限の数値で指定してください', 400);
    }
    return $value;
}

function normalizeRow(array $row): array
{
    foreach (
        [
            'yield',
            'forecast_yield',
            'cagr3',
            'cagr5',
            'roe',
            'equity_ratio',
            'payout',
            'price',
        ]
        as $key
    ) {
        $row[$key] = isset($row[$key]) ? (float) $row[$key] : null;
    }
    foreach (['streak', 'streak_nd', 'streak_base', 'streak_nd_base'] as $key) {
        $row[$key] = isset($row[$key]) ? (int) $row[$key] : null;
    }
    return $row;
}

function fetchRows(PDOStatement $statement): array
{
    $rows = [];
    while ($row = $statement->fetch(PDO::FETCH_ASSOC)) {
        $rows[] = normalizeRow($row);
    }
    return $rows;
}

function applyRateLimit(PDO $database, int $dailyLimit): void
{
    $ip = substr((string) ($_SERVER['REMOTE_ADDR'] ?? 'unknown'), 0, 128);
    $date = gmdate('Y-m-d');
    try {
        $database->exec('BEGIN IMMEDIATE');
        $statement = $database->prepare(
            'INSERT INTO hits(ip, date, count) VALUES(:ip, :date, 1)
             ON CONFLICT(ip, date) DO UPDATE SET count = count + 1'
        );
        $statement->execute([':ip' => $ip, ':date' => $date]);
        $statement = $database->prepare(
            'SELECT count FROM hits WHERE ip = :ip AND date = :date'
        );
        $statement->execute([':ip' => $ip, ':date' => $date]);
        $count = (int) $statement->fetchColumn();
        $database->exec('COMMIT');
    } catch (Throwable $exception) {
        if ($database->inTransaction()) {
            $database->rollBack();
        } else {
            try {
                $database->exec('ROLLBACK');
            } catch (Throwable) {
                // 元の例外を優先する。
            }
        }
        throw $exception;
    }
    if ($count > $dailyLimit) {
        errorResponse('本日のアクセス上限に達しました。しばらくしてからお試しください', 429);
    }
}

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'GET') {
    header('Allow: GET');
    errorResponse('GETメソッドのみ利用できます', 405);
}

try {
    $config = require __DIR__ . '/config.php';
    if (
        !is_array($config)
        || !isset($config['database_path'], $config['daily_limit'])
    ) {
        throw new RuntimeException('設定が不正です');
    }

    $databasePath = (string) $config['database_path'];
    if (!is_file($databasePath)) {
        throw new RuntimeException('データベースがありません');
    }
    $database = new PDO('sqlite:' . $databasePath, null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES => false,
    ]);
    $database->exec('PRAGMA busy_timeout = 5000');
    applyRateLimit($database, (int) $config['daily_limit']);

    $action = getString('action');
    // streak_base / streak_nd_base（実質連続増配・実質累進配当）は、cagr5と同じく
    // 独立列を持たず、記念・特別配当の内訳(payload.streakBase等)から都度読む。
    $rowSelect = <<<'SQL'
        SELECT code, name, industry, yield, forecast_yield,
               streak, streak_nd, cagr3,
               json_extract(payload, '$.cagr5') AS cagr5,
               json_extract(payload, '$.streakBase') AS streak_base,
               json_extract(payload, '$.streakNoDecreaseBase') AS streak_nd_base,
               roe, equity_ratio, payout, price
        FROM stocks
        SQL;

    if ($action === 'search') {
        $query = getString('q');
        $length = unicodeLength($query);
        if ($length < 2) {
            errorResponse('検索語は2文字以上で入力してください', 400);
        }
        if ($length > 80) {
            errorResponse('検索語が長すぎます', 400);
        }
        $escaped = escapedLike($query);
        $codePrefix = strtoupper($escaped) . '%';
        $namePart = '%' . $escaped . '%';
        $statement = $database->prepare(
            "SELECT code, name, industry
             FROM stocks
             WHERE code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'
             ORDER BY
               CASE WHEN code = ? THEN 0
                    WHEN code LIKE ? ESCAPE '\\' THEN 1
                    ELSE 2 END,
               code
             LIMIT 10"
        );
        $statement->execute([
            $codePrefix,
            $namePart,
            strtoupper($query),
            $codePrefix,
        ]);
        respond($statement->fetchAll());
    }

    if ($action === 'stock') {
        $code = strtoupper(getString('code'));
        if (preg_match('/^[0-9A-Z]{4}$/D', $code) !== 1) {
            errorResponse('銘柄コードは4桁の英数字で指定してください', 400);
        }
        $statement = $database->prepare(
            'SELECT payload FROM stocks WHERE code = :code LIMIT 1'
        );
        $statement->execute([':code' => $code]);
        $payload = $statement->fetchColumn();
        if ($payload === false) {
            errorResponse('指定された銘柄が見つかりません', 404);
        }
        $decoded = json_decode((string) $payload, true, 512, JSON_THROW_ON_ERROR);
        respond($decoded, 200, 'public, max-age=3600');
    }

    if ($action === 'list') {
        $sortExpressions = [
            'yield' => 'yield',
            'forecast_yield' => 'forecast_yield',
            'streak' => 'streak',
            'streak_nd' => 'streak_nd',
            'streak_base' => "json_extract(payload, '$.streakBase')",
            'streak_nd_base' => "json_extract(payload, '$.streakNoDecreaseBase')",
            'cagr3' => 'cagr3',
            'cagr5' => "json_extract(payload, '$.cagr5')",
            'roe' => 'roe',
            'equity_ratio' => 'equity_ratio',
            'payout' => 'payout',
            'price' => 'price',
            'code' => 'code',
            'name' => 'name',
        ];
        $sort = getString('sort', 'streak');
        if (!isset($sortExpressions[$sort])) {
            errorResponse('指定された並び順は利用できません', 400);
        }
        $order = strtolower(getString('order', 'desc'));
        if (!in_array($order, ['asc', 'desc'], true)) {
            errorResponse('order は asc または desc で指定してください', 400);
        }
        $limitRaw = getString('limit', '100');
        if (filter_var($limitRaw, FILTER_VALIDATE_INT) === false) {
            errorResponse('limit は整数で指定してください', 400);
        }
        $limit = max(1, min(300, (int) $limitRaw));
        $nullOrder = $order === 'desc' ? 'DESC' : 'ASC';
        $sql = $rowSelect
            . ' ORDER BY (' . $sortExpressions[$sort] . ' IS NULL) ASC, '
            . $sortExpressions[$sort] . ' ' . strtoupper($order)
            . ', code ' . $nullOrder
            . ' LIMIT :limit';
        $statement = $database->prepare($sql);
        $statement->bindValue(':limit', $limit, PDO::PARAM_INT);
        $statement->execute();
        respond(fetchRows($statement), 200, 'public, max-age=3600');
    }

    if ($action === 'screen') {
        $conditions = [];
        $parameters = [];
        $industry = getString('industry');
        if ($industry !== '') {
            if (unicodeLength($industry) > 80) {
                errorResponse('業種名が長すぎます', 400);
            }
            $conditions[] = 'industry = :industry';
            $parameters[':industry'] = $industry;
        }
        $yieldMin = numericParameter('yield_min');
        if ($yieldMin !== null) {
            $conditions[] = 'yield >= :yield_min';
            $parameters[':yield_min'] = $yieldMin;
        }
        $roeMin = numericParameter('roe_min');
        if ($roeMin !== null) {
            $conditions[] = 'roe >= :roe_min';
            $parameters[':roe_min'] = $roeMin;
        }
        $equityMin = numericParameter('equity_min');
        if ($equityMin !== null) {
            $conditions[] = 'equity_ratio >= :equity_min';
            $parameters[':equity_min'] = $equityMin;
        }
        $payoutMax = numericParameter('payout_max');
        if ($payoutMax !== null) {
            $conditions[] = 'payout <= :payout_max';
            $parameters[':payout_max'] = $payoutMax;
        }

        $where = $conditions ? ' WHERE ' . implode(' AND ', $conditions) : '';
        $countStatement = $database->prepare('SELECT COUNT(*) FROM stocks' . $where);
        $countStatement->execute($parameters);
        $total = (int) $countStatement->fetchColumn();

        $statement = $database->prepare(
            $rowSelect . $where
            . ' ORDER BY (streak IS NULL) ASC, streak DESC, yield DESC, code ASC LIMIT 300'
        );
        $statement->execute($parameters);
        respond(['total' => $total, 'rows' => fetchRows($statement)]);
    }

    if ($action === 'sectors') {
        $statement = $database->prepare('SELECT payload FROM sectors LIMIT 1');
        $statement->execute();
        $payload = $statement->fetchColumn();
        if ($payload === false) {
            throw new RuntimeException('業種統計がありません');
        }
        $decoded = json_decode((string) $payload, true, 512, JSON_THROW_ON_ERROR);
        respond($decoded, 200, 'public, max-age=3600');
    }

    errorResponse('指定されたactionは利用できません', 400);
} catch (JsonException) {
    errorResponse('保存データのJSON形式が不正です', 500);
} catch (Throwable $exception) {
    error_log('dividend checker API: ' . $exception->getMessage());
    errorResponse('サーバーでエラーが発生しました', 500);
}
