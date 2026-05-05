with open('schema.sql', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(r"\'ativo\'", "'ativo'")
content = content.replace(r"\'\'", "''")

with open('schema.sql', 'w', encoding='utf-8') as f:
    f.write(content)
