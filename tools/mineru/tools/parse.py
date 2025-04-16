import base64
import io
import json
import logging
import os
import time
import zipfile
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Dict

import httpx
from requests import post,get,put
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from dify_plugin.errors.tool import ToolProviderCredentialValidationError
from yarl import URL

logger = logging.getLogger(__name__)

@dataclass
class Credentials:
    base_url: str
    token: str
    server_type: str


class MineruTool(Tool):

    def _get_credentials(self) -> Credentials:
        """Get and validate credentials."""
        base_url = self.runtime.credentials.get("base_url")
        server_type = self.runtime.credentials.get("server_type")
        token = self.runtime.credentials.get("token")
        if not base_url:
            logger.error("Missing base_url in credentials")
            raise ToolProviderCredentialValidationError("Please input base_url")
        if server_type=="remote" and not token:
            logger.error("Missing token for remote server type")
            raise ToolProviderCredentialValidationError("Please input token")
        return Credentials(base_url=base_url, server_type=server_type, token=token)

    @staticmethod
    def _get_headers(credentials:Credentials) -> Dict[str, str]:
        """Get request headers."""
        if credentials.server_type=="remote":
            return {
                'Authorization': f'Bearer {credentials.token}',
                'Content-Type':'application/json',
            }
        return {
            'accept': 'application/json'
        }


    @staticmethod
    def _build_api_url(base_url: str, *paths: str) -> str:
        return str(URL(base_url) / "/".join(paths))

    def _invoke(self, tool_parameters: Dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        credentials = self._get_credentials()
        yield from self.parser_pdf(credentials, tool_parameters)

    def validate_token(self) -> None:
        """Validate URL and token."""
        credentials = self._get_credentials()
        if credentials.server_type=="local":
            url = self._build_api_url(credentials.base_url, "docs")
            logger.info(f"Validating local server connection to {url}")
            response = get(url, headers=self._get_headers(credentials),timeout=10)
            if response.status_code != 200:
                logger.error(f"Local server validation failed with status {response.status_code}")
                raise ToolProviderCredentialValidationError("Please check your base_url")
        elif credentials.server_type=="remote":
            url = self._build_api_url(credentials.base_url, "api/v4/file-urls/batch")
            logger.info(f"Validating remote server connection to {url}")
            response = post(url, headers=self._get_headers(credentials),timeout=10)
            if response.status_code!= 200:
                logger.error(f"Remote server validation failed with status {response.status_code}")
                raise ToolProviderCredentialValidationError("Please check your base_url and token")

    def _parser_pdf_local(self, credentials: Credentials, tool_parameters: Dict[str, Any]):
        """Parse PDF files by local server."""
        file = tool_parameters.get("file")
        if not file:
            logger.error("No file provided for PDF parsing")
            raise ValueError("File is required")

        headers = self._get_headers(credentials)
        task_url = self._build_api_url(credentials.base_url, "pdf_parse")
        logger.info(f"Starting PDF parse request to {task_url}")
        params = {
            'parse_method': tool_parameters.get('parse_method', 'auto'),
            'return_layout': False,
            'return_info': False,
            'return_content_list': True,
            'return_images': True
        }

        file_data = {
            "pdf_file": (file.filename, file.blob, 'application/pdf'),
        }
        response = post(task_url, headers=headers, params=params, files=file_data)
        if response.status_code != 200:
            logger.error(f"PDF parse failed with status {response.status_code}")
            yield self.create_text_message(f"Failed to parse PDF.result: {response.text}")
            return
        logger.info("PDF parse completed successfully")
        response_json = response.json()
        md_content = response_json.get("md_content", "")
        content_list = response_json.get("content_list", [])
        file_obj = response_json.get("images", {})

        images = []
        for file_name, encoded_image_data in file_obj.items():
            base64_data = encoded_image_data.split(",")[1]
            image_bytes = base64.b64decode(base64_data)
            file_res = self.session.file.upload(
                file_name,
                image_bytes,
                "image/jpeg"
            )
            images.append(file_res)

        yield self.create_variable_message("images", images)
        yield self.create_text_message(md_content)
        yield self.create_json_message({"content_list": content_list})

    def _parser_pdf_remote(self, credentials: Credentials, tool_parameters: Dict[str, Any]):
        """Parse PDF files by remote server."""
        file = tool_parameters.get("file")
        header = self._get_headers(credentials)

        # create parsing task
        data = {
            "enable_formula": tool_parameters.get("enable_formula", True),
            "enable_table": tool_parameters.get("enable_table", True),
            "language": tool_parameters.get("language", "auto"),
            "layout_model": tool_parameters.get("layout_model", "doclayout_yolo"),
            "files": [
                {"name": file.filename, "is_ocr": tool_parameters.get("enable_ocr",False)}
            ]
        }
        task_url = self._build_api_url(credentials.base_url, "api/v4/file-urls/batch")
        response = post(task_url, headers=header, json=data)

        if response.status_code != 200:
            logger.error('apply upload url failed. status:{} ,result:{}'.format(response.status_code, response.text))
            raise Exception('apply upload url failed. status:{} ,result:{}'.format(response.status_code, response.text))

        result = response.json()

        if result["code"] == 0:
            logger.info('apply upload url success,result:{}'.format(result))
            batch_id = result["data"]["batch_id"]
            urls = result["data"]["file_urls"]

            res_upload = put(urls[0], data=file.blob)
            if res_upload.status_code == 200:
                logger.info(f"{urls[0]} upload success")
            else:
                logger.error(f"{urls[0]} upload failed")
                raise Exception(f"{urls[0]} upload failed")

            extract_result = self._poll_get_parse_result(credentials, batch_id)

            zip_result = self._download_and_extract_zip(extract_result.get("full_zip_url"))

            md_content = zip_result.get("md_content", "")
            content_list = zip_result.get("content_list", [])
            images = zip_result.get("images", [])
            md_content = self._replace_md_img_path(md_content, images)

            yield self.create_text_message(md_content)
            yield self.create_json_message({"content_list": content_list,"full_zip_url":extract_result.get("full_zip_url")})
            yield self.create_variable_message("images", images)
        else:
            logger.error('apply upload url failed,reason:{}'.format(result.msg))
            raise Exception('apply upload url failed,reason:{}'.format(result.msg))

    def _poll_get_parse_result(self, credentials: Credentials, batch_id: str) -> Dict[str, Any]:
        """poll get parser result."""
        url = self._build_api_url(credentials.base_url, f"api/v4/extract-results/batch/{batch_id}")
        headers = self._get_headers(credentials)
        max_retries = 50
        retry_interval = 5

        for _ in range(max_retries):
            response = get(url, headers=headers)
            if response.status_code == 200:
                data = response.json().get("data", {})
                extract_result = data.get("extract_result", {})[0]
                if extract_result.get("state") == "done":
                    logger.info("Parse completed successfully")
                    return extract_result
                if extract_result.get("state") == "failed":
                    logger.error(f"Parse failed, reason: {extract_result.get('err_msg')}")
                    raise Exception(f"Parse failed, reason: {extract_result.get('err_msg')}")
                logger.info(f"Parse in progress, state: {extract_result.get('state')}")
            else:
                logger.warning(f"Failed to get parse result, status: {response.status_code}")
                raise Exception(f"Failed to get parse result, status: {response.status_code}")

            time.sleep(retry_interval)

        logger.error("Polling timeout reached without getting completed result")
        raise TimeoutError("Parse operation timed out")

    def _download_and_extract_zip(self, url: str) -> dict[str, Any]:
        """Download and extract zip file from URL."""
        response = httpx.get(url)
        response.raise_for_status()
        zip_content = response.content
        images = []
        md_content = ""
        content_list = []
        with zipfile.ZipFile(io.BytesIO(zip_content)) as zip_file:
            for file_info in zip_file.infolist():
                file_name = file_info.filename
                if not file_info.is_dir():
                    if file_name.lower().startswith("images/") and file_name.lower().endswith(('.png', '.jpg', '.jpeg')):
                            with zip_file.open(file_info) as image_file:
                                image_content = image_file.read()
                                base_name = os.path.basename(file_name)
                                file_res = self.session.file.upload(
                                    base_name,
                                    image_content,
                                    "image/jpeg"
                                )
                                images.append(file_res)
                    elif file_name.lower() == "full.md":
                        with zip_file.open(file_info) as md_file:
                            md_content = md_file.read().decode('utf-8')
                    elif file_name.lower().endswith('.json') and file_name.lower()!="layout.json":
                        with zip_file.open(file_info) as json_file:
                            json_content = json_file.read().decode('utf-8')
                            content_list.append(json.loads(json_content))

        return {"md_content": md_content, "content_list": content_list, "images": images}

    @staticmethod
    def _replace_md_img_path(md_content: str, images: list) -> str:
        for image in images:
            md_content = md_content.replace("images/"+image.name, image.preview_url)
        return md_content

    def parser_pdf(
        self,
        credentials: Credentials,
        tool_parameters: Dict[str, Any]
    ) -> Generator[ToolInvokeMessage, None, None]:
        if credentials.server_type=="local":
            yield from self._parser_pdf_local(credentials, tool_parameters)
        elif credentials.server_type=="remote":
            yield from self._parser_pdf_remote(credentials, tool_parameters)


