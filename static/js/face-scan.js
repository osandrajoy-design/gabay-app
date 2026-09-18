const camera = document.getElementById("camera");

const startCameraButton =
    document.getElementById("startCamera");

const captureFaceButton =
    document.getElementById("captureFace");

const retakeFaceButton =
    document.getElementById("retakeFace");

const cameraMessage =
    document.getElementById("cameraMessage");

const previewContainer =
    document.getElementById("previewContainer");

const facePreview =
    document.getElementById("facePreview");

const faceImage =
    document.getElementById("faceImage");

let cameraStream = null;
let faceDetectionInterval = null;

let faceDetected = false;
let detectedFace = null;


/* =========================
   LOAD FACE MODEL
========================= */

async function loadFaceModels() {

    cameraMessage.textContent =
        "Loading face detection...";

    try {

        await faceapi.nets.tinyFaceDetector.loadFromUri(
            "/static/models"
        );

        cameraMessage.textContent =
            "Face detection ready.";

    } catch (error) {

        console.error(error);

        cameraMessage.textContent =
            "Unable to load face detection.";

    }
}


/* =========================
   START CAMERA
========================= */

startCameraButton.addEventListener(
    "click",
    async () => {

        try {

            await loadFaceModels();

            cameraStream =
                await navigator.mediaDevices.getUserMedia({
                    video: {
                        facingMode: "user"
                    },
                    audio: false
                });

            camera.srcObject =
                cameraStream;

            cameraMessage.textContent =
                "Position your face inside the guide.";

            startCameraButton.disabled =
                true;

            captureFaceButton.disabled =
                true;

            startFaceDetection();

        } catch (error) {

            console.error(error);

            cameraMessage.textContent =
                "Unable to access camera.";

            alert(
                "Camera access was denied or is unavailable."
            );

        }

    }
);


/* =========================
   FACE DETECTION
========================= */

function startFaceDetection() {

    if (faceDetectionInterval) {
        clearInterval(faceDetectionInterval);
    }

    faceDetectionInterval =
        setInterval(async () => {

            if (
                camera.readyState !==
                HTMLMediaElement.HAVE_ENOUGH_DATA
            ) {
                return;
            }

            try {

                const detection =
                    await faceapi.detectSingleFace(
                        camera,
                        new faceapi.TinyFaceDetectorOptions({
                            inputSize: 320,
                            scoreThreshold: 0.5
                        })
                    );

                if (!detection) {

                    faceDetected = false;
                    detectedFace = null;

                    captureFaceButton.disabled =
                        true;

                    cameraMessage.textContent =
                        "No face detected.";

                    return;
                }


                const box =
                    detection.box;

                const videoWidth =
                    camera.videoWidth;

                const videoHeight =
                    camera.videoHeight;


                const faceCenterX =
                    box.x + box.width / 2;

                const faceCenterY =
                    box.y + box.height / 2;


                const screenCenterX =
                    videoWidth / 2;

                const screenCenterY =
                    videoHeight / 2;


                const horizontalDifference =
                    Math.abs(
                        faceCenterX -
                        screenCenterX
                    );

                const verticalDifference =
                    Math.abs(
                        faceCenterY -
                        screenCenterY
                    );


                const horizontallyCentered =
                    horizontalDifference <
                    videoWidth * 0.15;

                const verticallyCentered =
                    verticalDifference <
                    videoHeight * 0.15;


                if (
                    horizontallyCentered &&
                    verticallyCentered
                ) {

                    faceDetected = true;

                    detectedFace = box;

                    captureFaceButton.disabled =
                        false;

                    cameraMessage.textContent =
                        "Face detected. You may capture.";

                } else {

                    faceDetected = false;
                    detectedFace = null;

                    captureFaceButton.disabled =
                        true;

                    cameraMessage.textContent =
                        "Please center your face.";

                }

            } catch (error) {

                console.error(
                    "Face detection error:",
                    error
                );

            }

        }, 300);
}


/* =========================
   CAPTURE CENTERED FACE
========================= */

captureFaceButton.addEventListener(
    "click",
    () => {

        if (
            !cameraStream ||
            !faceDetected ||
            !detectedFace
        ) {
            return;
        }


        const canvas =
            document.createElement("canvas");


        /*
            Create a square profile image.
        */

        const outputSize = 500;

        canvas.width =
            outputSize;

        canvas.height =
            outputSize;


        const context =
            canvas.getContext("2d");


        /*
            Add some space around the face.
        */

        const padding = 80;


        let cropX =
            detectedFace.x -
            padding;

        let cropY =
            detectedFace.y -
            padding;

        let cropWidth =
            detectedFace.width +
            padding * 2;

        let cropHeight =
            detectedFace.height +
            padding * 2;


        /*
            Keep crop inside camera frame.
        */

        if (cropX < 0) {
            cropX = 0;
        }

        if (cropY < 0) {
            cropY = 0;
        }

        if (
            cropX +
            cropWidth >
            camera.videoWidth
        ) {

            cropWidth =
                camera.videoWidth -
                cropX;

        }

        if (
            cropY +
            cropHeight >
            camera.videoHeight
        ) {

            cropHeight =
                camera.videoHeight -
                cropY;

        }


        /*
            Mirror the camera image.
        */

        context.translate(
            outputSize,
            0
        );

        context.scale(
            -1,
            1
        );


        /*
            Draw only the detected face area.
        */

        context.drawImage(
            camera,
            cropX,
            cropY,
            cropWidth,
            cropHeight,

            0,
            0,
            outputSize,
            outputSize
        );


        const imageData =
            canvas.toDataURL(
                "image/jpeg",
                0.90
            );


        facePreview.src =
            imageData;

        faceImage.value =
            imageData;


        previewContainer.style.display =
            "block";


        cameraMessage.textContent =
            "Profile photo captured successfully.";

    }
);


/* =========================
   RETAKE
========================= */

retakeFaceButton.addEventListener(
    "click",
    () => {

        faceImage.value = "";

        facePreview.src = "";

        previewContainer.style.display =
            "none";

        faceDetected = false;

        detectedFace = null;

        captureFaceButton.disabled =
            true;

        cameraMessage.textContent =
            "Position your face inside the guide.";

    }
);