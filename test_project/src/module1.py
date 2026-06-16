class Module1:
    """Main module class with enhanced functionality."""
    
    def __init__(self, initial_value=42):
        self.value = initial_value
    
    def increment(self, amount=1):
        self.value += amount
        return self.value
    
    def get_value(self):
        return self.value
