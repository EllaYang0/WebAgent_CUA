import json
import re
from datetime import datetime, timedelta
from pathlib import Path


def parse_date_value(date_val: datetime, options: dict) -> str:
    """将 datetime 对象按照 options 格式化为字符串"""
    month_fmt = options.get('month', 'short')
    year_set = options.get('year', 'set') == 'set'

    if month_fmt == 'long':
        month_str = date_val.strftime('%B')
    else:
        month_str = date_val.strftime('%b')

    day_str = str(date_val.day)

    if year_set:
        return f"{month_str} {day_str}, {date_val.year}"
    else:
        return f"{month_str} {day_str}"


def parse_date_expression(expr: str, now: datetime):
    """
    解析日期表达式，返回：
    - 单个日期字符串（普通日期）
    - 或 dict {"0": "出发日", "1": "返回日", "_display": "出发 - 返回"}（range）

    支持格式：
      "{now() + timedelta(309)} | month=long | prefix=none | year=set"
      "{now() + timedelta(300, 303)} | prefix=none | range=endpoints | year=set"
    """
    expr = expr.strip()
    parts = [p.strip() for p in expr.split('|')]
    date_expr = parts[0].strip('{}').strip()
    options = {}
    for part in parts[1:]:
        if '=' in part:
            k, v = part.split('=', 1)
            options[k.strip()] = v.strip()

    is_range = options.get('range') == 'endpoints'
    local_vars = {'now': lambda: now, 'timedelta': timedelta}

    if is_range:
        match = re.search(r'timedelta\((\d+),\s*(\d+)\)', date_expr)
        if not match:
            raise ValueError(f"range 模式下无法解析 timedelta: {date_expr}")
        date_start = now + timedelta(int(match.group(1)))
        date_end = now + timedelta(int(match.group(2)))
        s = parse_date_value(date_start, options)
        e = parse_date_value(date_end, options)
        return {"0": s, "1": e, "_display": f"{s} - {e}"}
    else:
        date_val = eval(date_expr, {"__builtins__": {}}, local_vars)
        return parse_date_value(date_val, options)


def resolve_values(values: dict, now: datetime) -> dict:
    """
    解析所有 values，range 类型额外展开 key.0 / key.1
    """
    resolved = {}
    for key, expr in values.items():
        if isinstance(expr, str) and ('{' in expr or 'now()' in expr):
            result = parse_date_expression(expr, now)
            if isinstance(result, dict):
                resolved[key] = result["_display"]
                resolved[f"{key}.0"] = result["0"]
                resolved[f"{key}.1"] = result["1"]
            else:
                resolved[key] = result
        else:
            resolved[key] = expr
    return resolved


def replace_gt_info_dates(gt_info: list, resolved: dict) -> list:
    """将 gt_info segments 中的 date 引用替换为真实日期"""
    result = []
    for item in gt_info:
        new_item = dict(item)
        new_segments = []
        for seg in item.get('segments', []):
            new_seg = dict(seg)
            date_ref = seg.get('date', '')
            if date_ref in resolved:
                new_seg['date'] = resolved[date_ref]
            new_segments.append(new_seg)
        new_item['segments'] = new_segments
        result.append(new_item)
    return result


def preprocess_task(raw: dict, now: datetime = None) -> dict:
    if now is None:
        now = datetime.now()

    config = json.loads(raw['task_generation_config_json'])
    values = config.get('values', {})
    resolved = resolve_values(values, now)

    task_text = config['task']
    for key, val in resolved.items():
        task_text = task_text.replace(f'{{{key}}}', val)

    gt_info = config.get('gt_info', [])
    gt_info_resolved = replace_gt_info_dates(gt_info, resolved)

    target = config.get('_target_', '')
    is_nav = 'search_match' in target or 'url_match' in target

    return {
        "task_id": raw['task_id'],
        "task": task_text,
        "start_url": config['url'],
        "gt_urls": config.get('gt_urls', config.get('gt_url', [])),  # 从config读
        "gt_info": gt_info_resolved,
        "resolved_values": resolved,
        "domain": raw.get('domain'),
        "l2_category": raw.get('l2_category'),
        "target": target,
        "task_type": "url_navigation" if is_nav else "info_extraction",
    }

def preprocess_file(input_path: str, output_path: str):
    now = datetime.now()
    results = []

    with open(input_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            try:
                processed = preprocess_task(raw, now)
                results.append(processed)
                print(f"✅ {processed['task_id']}")
                print(f"   task: {processed['task'][:100]}...")
                if processed['gt_info']:
                    print(f"   gt_info[0].segments: {processed['gt_info'][0]['segments']}")
            except Exception as e:
                import traceback
                print(f"❌ {raw.get('task_id', 'unknown')}: {e}")
                traceback.print_exc()

    with open(output_path, 'w') as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    print(f"\n共处理 {len(results)} 条任务，输出到 {output_path}")


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print("Usage: python preprocess_tasks.py <input.jsonl> <output.jsonl>")
        sys.exit(1)
    preprocess_file(sys.argv[1], sys.argv[2])