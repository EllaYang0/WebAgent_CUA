from datasets import load_dataset
import json, os, re
from datetime import datetime, timedelta


def parse_date_value(date_val: datetime, options: dict) -> str:
    month_fmt = options.get('month', 'short')
    year_set = options.get('year', 'set') == 'set'
    month_str = date_val.strftime('%B') if month_fmt == 'long' else date_val.strftime('%b')
    day_str = str(date_val.day)
    return f"{month_str} {day_str}, {date_val.year}" if year_set else f"{month_str} {day_str}"


def parse_date_expression(expr: str, now: datetime):
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


def preprocess_task(raw: dict, now: datetime) -> dict:
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
        "gt_urls": config.get('gt_urls', config.get('gt_url', [])),
        "gt_info": gt_info_resolved,
        "resolved_values": resolved,
        "domain": raw.get('domain'),
        "l2_category": raw.get('l2_category'),
        "target": target,
        "task_type": "url_navigation" if is_nav else "info_extraction",
    }


if __name__ == '__main__':
    dataset = load_dataset("yutori-ai/navi-bench", split="validation")
    print(f"数据集总条数: {len(dataset)}")

    first_50 = dataset.select(range(40, 60))
    now = datetime.now()

    save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "navi_bench_first50_ready.jsonl")

    with open(save_path, "w") as f:
        for item in first_50:
            try:
                processed = preprocess_task(dict(item), now)
                f.write(json.dumps(processed, ensure_ascii=False) + "\n")
                print(f"✅ {processed['task_id']}: {processed['task'][:80]}")
            except Exception as e:
                print(f"❌ {item.get('task_id')}: {e}")

    print(f"保存完成，路径: {save_path}")