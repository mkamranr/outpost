def rms(values):
    return math.sqrt(sum(v * v for v in values) / len(values))
