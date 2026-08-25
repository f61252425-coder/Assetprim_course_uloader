FROM python:3.10-slim

# Install Node.js & npm (for megajs)
RUN apt-get update && apt-get install -y nodejs npm

# Set working directory
WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install megajs module
RUN npm init -y && npm install megajs

# Copy all project files
COPY . .

# Run the python app
CMD ["python", "main.py"]
