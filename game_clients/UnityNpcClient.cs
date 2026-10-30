// Unity · NPC 对话客户端（C#）
// 用法：
//   1. 先启动后端： python server.py
//   2. 把这个脚本挂到场景里任意一个 GameObject 上
//   3. 协程里调用： yield return StartCoroutine(npc.Talk("你好", r => Debug.Log(r)));
//
// 原理：Unity 通过 HTTP 请求本地 Python 服务，Python 那边跑智能体。

using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

public class NpcClient : MonoBehaviour
{
    public string server = "http://127.0.0.1:8200";
    public string npcKey = "blacksmith";
    private string sessionId;

    void Awake()
    {
        sessionId = "unity-" + Guid.NewGuid().ToString("N").Substring(0, 8);
    }

    [Serializable]
    private class ChatRequest
    {
        public string session;
        public string mode = "npc";
        public string npc;
        public string message;
    }

    [Serializable]
    private class ChatResponse
    {
        public string reply;
        public string error;
        public GameState state;
    }

    [Serializable]
    public class GameState
    {
        public int player_hp;
        public int gold;
        public string[] inventory;
    }

    /// <summary>跟 NPC 说一句话，回复通过回调返回。</summary>
    public IEnumerator Talk(string message, Action<string> onReply)
    {
        var req = new ChatRequest
        {
            session = sessionId,
            mode = "npc",
            npc = npcKey,
            message = message,
        };
        string json = JsonUtility.ToJson(req);
        var body = Encoding.UTF8.GetBytes(json);

        using var www = new UnityWebRequest(server + "/chat", "POST");
        www.uploadHandler = new UploadHandlerRaw(body);
        www.downloadHandler = new DownloadHandlerBuffer();
        www.SetRequestHeader("Content-Type", "application/json");

        yield return www.SendWebRequest();

        if (www.result != UnityWebRequest.Result.Success)
        {
            Debug.LogError("请求失败: " + www.error);
            onReply?.Invoke("");
            yield break;
        }

        var resp = JsonUtility.FromJson<ChatResponse>(www.downloadHandler.text);
        if (resp == null || !string.IsNullOrEmpty(resp.error))
        {
            onReply?.Invoke("");
            yield break;
        }

        // 对话可能已经改动了游戏状态，这里同步回来
        if (resp.state != null)
            ApplyState(resp.state);

        onReply?.Invoke(resp.reply);
    }

    private void ApplyState(GameState s)
    {
        // 把 state 映射到你自己的游戏数据上
        Debug.Log($"HP={s.player_hp} 金币={s.gold} 背包={string.Join(",", s.inventory)}");
    }
}
