lines = open('Main.qml', encoding='utf-8').readlines()
depth = 0
for i, line in enumerate(lines, 1):
    in_str = False
    for c in line:
        if c == '"':
            in_str = not in_str
        if not in_str:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
    if 1322 <= i <= 1500:
        print("Line %d depth=%d: %s" % (i, depth, line.rstrip()[:90]))
print("Final depth: %d" % depth)
