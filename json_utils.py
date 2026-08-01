import json


def load_json(filepath: str) -> dict:
    '''读取 JSON 文件并返回字典.

    Args:
        filepath: JSON 文件的路径.

    Returns:
        解析后的字典.

    Raises:
        FileNotFoundError: 文件不存在时.
        json.JSONDecodeError: JSON 格式无效时.
    '''
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)
