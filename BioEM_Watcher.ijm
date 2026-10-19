// BIO-EM AUTOMATIC WATCHER
print("Bio-EM Watcher: Online & Listening... ...");

triggerFile = "D:/fiji_trigger.txt";
mosaicScript = getDirectory("imagej") + "macros/BioEM_Mosaic.py";

while (true) {
    if (File.exists(triggerFile)) {
        rawContent = File.openAsString(triggerFile);
        cleanContent = trim(rawContent);
        
        // Delete trigger immediately
        File.delete(triggerFile);
        
        if (lengthOf(cleanContent) > 5) {
            parts = split(cleanContent, "|");
            targetPath = parts[0];
            
            projectName = "EM_Project";
            mode = "z-stack";
            rows = "1";
            cols = "1";
            
            if (parts.length >= 5) {
                projectName = parts[1];
                mode = parts[2];
                rows = parts[3];
                cols = parts[4];
            }
            
            print("Action Triggered: " + projectName + " [" + mode + "]");

            // WORKFLOW 1: 2D MOSAIC & MOSAIC Z-STACK 
            if (mode == "mosaic" || mode == "mosaic-z" || mode == "mosaic-stack") {
                argString = targetPath + "|" + projectName + "|" + rows + "|" + cols + "|" + mode;
                runMacro(mosaicScript, argString);

            // WORKFLOW 2: STANDARD SINGLE-TILE Z-STACK
            } else {
                run("Image Sequence...", "open=[" + targetPath + "] sort use");
                run("Invert LUT");
            }
            print("Waiting for next sync...");
        }
    }
    wait(1000);
}