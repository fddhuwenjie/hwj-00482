def format_number(n):
    return f'{n:,}'

def truncate(s, max_len=50):
    if len(s) <= max_len:
        return s
    return s[:max_len - 3] + '...'
