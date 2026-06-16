class DataProcessor:
    def __init__(self, data):
        self.data = data
    
    def filter_positive(self):
        return [x for x in self.data if x > 0]
    
    def sum(self):
        return sum(self.data)
