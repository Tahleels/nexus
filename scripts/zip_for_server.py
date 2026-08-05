import os
import zipfile

def zip_project(folder_path, zip_path):
    # These are the bulky folders we do NOT need to send!
    ignore_dirs = {'venv', '__pycache__', '.git', '.nlq_schema_cache', '.vscode', '.idea'}
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_path):
            # Modify dirs in-place to skip ignored directories
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            
            for file in files:
                if file == "server_deployment.zip":
                    continue
                
                absolute_path = os.path.join(root, file)
                relative_path = os.path.relpath(absolute_path, folder_path)
                zipf.write(absolute_path, relative_path)

if __name__ == "__main__":
    print("Packing project without 'venv' and cache...")
    zip_project(".", "server_deployment.zip")
    
    # Calculate size
    size_mb = os.path.getsize("server_deployment.zip") / (1024 * 1024)
    print(f"Deployment Zip created successfully: server_deployment.zip ({size_mb:.2f} MB)")
