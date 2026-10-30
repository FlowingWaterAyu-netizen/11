# Godot 4 · NPC 对话客户端
# 用法：
#   1. 先启动后端： python server.py
#   2. 把这个脚本挂到一个 Node 上（比如挂在 NPC 场景的根节点）
#   3. 运行时调用 await npc.talk("你好")，拿到回复
#
# 原理：Godot 通过 HTTP 请求本地 Python 服务，Python 那边跑智能体。

extends Node

const SERVER := "http://127.0.0.1:8200"

# 每个 NPC 一个独立 session，这样记忆互不串台
@export var npc_key := "blacksmith"
@export var session_id := ""

var _http: HTTPRequest
var _pending := false


func _ready() -> void:
	_http = HTTPRequest.new()
	add_child(_http)
	_http.request_completed.connect(_on_completed)
	if session_id == "":
		session_id = "godot-" + str(randi())


## 跟 NPC 说一句话，返回它的回复（await npc.talk("...")）
func talk(message: String) -> String:
	if _pending:
		push_warning("上一条还没回复完")
		return ""
	_pending = true

	var body := JSON.stringify({
		"session": session_id,
		"mode": "npc",
		"npc": npc_key,
		"message": message,
	})
	var headers := ["Content-Type: application/json"]
	var err := _http.request(SERVER + "/chat", headers, HTTPClient.METHOD_POST, body)
	if err != OK:
		_pending = false
		push_error("请求失败: %s" % err)
		return ""

	var result: Array = await _http.request_completed
	_pending = false
	var json := JSON.parse_string(result[3].get_string_from_utf8())
	if json == null or json.has("error"):
		return "（NPC 没说话）"
	return json.get("reply", "")


## 读取 NPC 改动后的游戏状态（血量/金币/背包）
func fetch_state() -> Dictionary:
	var url := "%s/state?session=%s&mode=npc&npc=%s" % [SERVER, session_id, npc_key]
	var err := _http.request(url)
	if err != OK:
		return {}
	var result: Array = await _http.request_completed
	var json := JSON.parse_string(result[3].get_string_from_utf8())
	return json.get("state", {}) if json else {}


func _on_completed(_r, _c, _h, _b) -> void:
	pass  # 用 await 处理，这里留空
