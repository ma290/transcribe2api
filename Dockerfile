# Step 1: Use official Playwright Python base image (Ubuntu based, fully compatible)
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# Step 2: Set working directory inside the container
WORKDIR /app

# Step 3: Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 4: Install only the WebKit browser binary
# System dependencies are already pre-baked inside this official image!
RUN playwright install webkit

# Step 5: Copy the rest of your source code
COPY . .

# Step 6: Expose the target port for Koyeb
EXPOSE 8000

# Step 7: Spin up the single worker Uvicorn server (RAM optimized)
CMD ["uvicorn", "ihttp:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
