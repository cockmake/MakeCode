"""
QuickBI OpenAPI HTTP 调用工具函数

使用 HMAC-SHA256 签名方式调用 QuickBI OpenAPI，无需 SDK 依赖。
"""

import base64
import hmac
import time
import uuid
from urllib import parse
import requests
import yaml


def hash_hmac(key: str, code: str, algorithm: str = 'sha256') -> str:
    """Base64编码的HMAC-SHA256计算值"""
    hmac_code = hmac.new(key.encode('UTF-8'), code.encode('UTF-8'), algorithm).digest()
    return base64.b64encode(hmac_code).decode()


def build_signature(
    method: str,
    uri: str,
    params: dict,
    access_id: str,
    access_key: str,
    nonce: str,
    timestamp: str
) -> str:
    """
    构造签名
    
    StringToSign = HTTP_METHOD + "\n" + URI + QueryString + 
                   "\nX-Gw-AccessId:" + AccessID + 
                   "\nX-Gw-Nonce:" + UUID + 
                   "\nX-Gw-Timestamp:" + Timestamp
    Signature = Base64(HMAC-SHA256(AccessKey, URL_Encode(StringToSign)))
    """
    # Request参数拼接（按key排序）
    if not params:
        request_query_string = ''
    else:
        sorted_keys = sorted(params.keys())
        query_parts = [f"{key}={params[key]}" for key in sorted_keys if params[key] is not None]
        request_query_string = '\n' + '&'.join(query_parts) if query_parts else ''
    
    # Request Header拼接
    request_headers = f'\nX-Gw-AccessId:{access_id}\nX-Gw-Nonce:{nonce}\nX-Gw-Timestamp:{timestamp}'
    
    # 待签名字符串
    string_to_sign = method.upper() + '\n' + uri + request_query_string + request_headers
    
    # URL编码并计算签名
    encode_string = parse.quote(string_to_sign, '')
    sign = hash_hmac(access_key, encode_string)
    
    return sign


def call_quickbi_api(
    host: str,
    uri: str,
    access_id: str,
    access_key: str,
    method: str = "POST",
    json_param: dict = None,
    form_params: dict = None,
    content_type: str = "application/json"
) -> dict:
    """
    调用 QuickBI OpenAPI
    
    Args:
        host: QuickBI 服务域名
        uri: API 接口路径
        access_id: AccessKey ID
        access_key: AccessKey Secret
        method: HTTP 方法，默认 POST
        json_param: JSON 格式请求体
        form_params: 表单参数（参与签名计算）
        content_type: Content-Type，默认 application/json
    
    Returns:
        JSON 格式的响应数据
    """
    url = host + uri
    nonce = str(uuid.uuid1())
    timestamp = str(round(time.time() * 1000))
    
    signature = build_signature(method, uri, form_params, access_id, access_key, nonce, timestamp)
    
    headers = {
        'X-Gw-AccessId': access_id,
        'X-Gw-Nonce': nonce,
        'X-Gw-Timestamp': timestamp,
        'X-Gw-Signature': signature,
        'X-Gw-Debug': 'true',
        'Content-Type': content_type
    }
    
    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=form_params,
        json=json_param
    )
    return response.json()


def query_openapi(
    endpoint: str,
    access_key_id: str,
    access_key_secret: str,
    question: str,
    user_id: str = None,
    cube_id: str = None
) -> dict:
    """
    调用 QuickBI SmartQ 查询接口
    与 SDK 的 SmartqQueryAbility 接口入参保持一致
    
    Args:
        endpoint: QuickBI endpoint
        access_key_id: AccessKey ID
        access_key_secret: AccessKey Secret
        question: 自然语言问题
        user_id: 用户ID（可选）
        cube_id: 数据集ID（可选，多个用逗号分隔）
    
    Returns:
        查询结果 JSON
    """
    uri = "/openapi/v2/smartq/queryByQuestion"
    
    json_param = {"userQuestion": question}
    
    if user_id:
        json_param["userId"] = user_id
    
    # 处理 cube_id（单表/多表场景）
    if cube_id:
        if ',' in cube_id:
            json_param["multipleCubeIds"] = cube_id  # 多表
        else:
            json_param["cubeId"] = cube_id  # 单表
    
    return call_quickbi_api(
        host=endpoint,
        uri=uri,
        access_id=access_key_id,
        access_key=access_key_secret,
        method="POST",
        json_param=json_param
    )


def load_config(config_path: str) -> dict:
    """加载 config.yaml 配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def extract_page_id(url: str) -> str:
    """
    从仪表板 URL 中提取 pageId
    
    支持格式:
    - https://bi.aliyun.com/dashboard/view/pc.htm?pageId=XXXXXXX
    - https://pre-bi.aliyun.com/token3rd/dashboard/view/pc.htm?pageId=XXXXXXX&accessToken=...
    
    Args:
        url: 仪表板 URL
    
    Returns:
        pageId 字符串
    
    Raises:
        ValueError: 如果无法提取 pageId
    """
    import re
    match = re.search(r'pageId=([a-zA-Z0-9-]+)', url)
    if match:
        return match.group(1)
    raise ValueError(f"无法从 URL 中提取 pageId: {url}")


def validate_and_prepare_dashboard(
    host: str,
    access_id: str,
    access_key: str,
    page_id: str,
    user_id: str
) -> dict:
    """
    仪表板转换前的预校验及预处理
    
    Args:
        host: QuickBI 服务域名
        access_id: API Key
        access_key: API Secret
        page_id: 仪表板 pageId
        user_id: 用户 token
    
    Returns:
        {
            "success": True,
            "url": "预处理后的仪表板 URL"
        }
        或
        {
            "success": False,
            "error_code": "错误码",
            "error_message": "错误信息"
        }
    """
    uri = "/openapi/v2/skills/dashboard/handle"
    json_param = {
        "id": page_id,
        "userId": user_id
    }
    
    try:
        result = call_quickbi_api(
            host=host,
            uri=uri,
            access_id=access_id,
            access_key=access_key,
            method="POST",
            json_param=json_param
        )
        
        # success 可能是布尔值或字符串 "true"/"false"
        success_val = result.get("success")
        is_success = success_val == True or success_val == "true"
        
        if is_success:
            # URL 在 data 字段中
            return {
                "success": True,
                "url": result.get("data")
            }
        else:
            return {
                "success": False,
                "error_code": str(result.get("errorCode", result.get("code", "UNKNOWN"))),
                "error_message": result.get("errorMsg", result.get("message", "未知错误"))
            }
    except Exception as e:
        return {
            "success": False,
            "error_code": "CONNECTION_ERROR",
            "error_message": f"连接失败: {str(e)}"
        }


def validate_api_credentials(config: dict) -> dict:
    """
    验证 API 凭证有效性
    
    Args:
        config: 配置字典，需包含 endpoint, access_key_id, access_key_secret
    
    Returns:
        验证结果 {"success": bool, "error": str, "error_code": str}
    """
    try:
        # 调用一个简单的接口验证凭证
        result = call_quickbi_api(
            host=config.get("endpoint", "https://quickbi-public.cn-hangzhou.aliyuncs.com"),
            uri="/openapi/v2/workspace/list",
            access_id=config["access_key_id"],
            access_key=config["access_key_secret"],
            method="GET"
        )
        
        if result.get("success", False) or result.get("code") == 200:
            return {"success": True}
        else:
            return {
                "success": False,
                "error": result.get("message", "API 验证失败"),
                "error_code": str(result.get("code", "UNKNOWN"))
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"连接失败: {str(e)}",
            "error_code": "CONNECTION_ERROR"
        }


# 使用示例
if __name__ == "__main__":
    # 加载配置
    config = load_config("../config.yaml")
    
    # 验证凭证
    validation = validate_api_credentials(config)
    if not validation["success"]:
        print(f"验证失败: {validation['error']}")
        exit(1)
    
    # SmartQ 查询
    result = query_openapi(
        endpoint=config["endpoint"],
        access_key_id=config["access_key_id"],
        access_key_secret=config["access_key_secret"],
        question="查询销售额排名前五的商品",
        cube_id="your-cube-id"
    )
    print(result)
