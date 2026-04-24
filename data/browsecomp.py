import pandas as pd
import base64
import hashlib
import json

# ===== 解密函数（与 BrowseCompEval 完全一致） =====

def derive_key(password: str, length: int) -> bytes:
    hasher = hashlib.sha256()
    hasher.update(password.encode())
    key = hasher.digest()
    return key * (length // len(key)) + key[: length % len(key)]

def decrypt(ciphertext_b64: str, password: str) -> str:
    encrypted = base64.b64decode(ciphertext_b64)
    key = derive_key(password, len(encrypted))
    decrypted = bytes(a ^ b for a, b in zip(encrypted, key))
    return decrypted.decode()

# ===== 读取 BrowseComp CSV =====

df = pd.read_csv(
    "https://openaipublic.blob.core.windows.net/simple-evals/browse_comp_test_set.csv"
)

# ✅ 修改 1: 改文件名
output_path = "browsecomp_first50.jsonl"

# ===== 解密并写入 jsonl =====

with open(output_path, "w", encoding="utf-8") as f:
    # ✅ 修改 2: 改循环范围
    for i in range(50):  # 从 10 改成 50
        row = df.iloc[i]
        question = decrypt(row["problem"], row["canary"])
        answer = decrypt(row["answer"], row["canary"])

        record = {
            "id": i,
            "question": question,
            "answer": answer
        }

        f.write(json.dumps(record, ensure_ascii=False) + "\n")

print(f"Saved first 50 BrowseComp examples to {output_path}")