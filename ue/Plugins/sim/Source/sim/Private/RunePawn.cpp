#include "RunePawn.h"

#include "Camera/CameraComponent.h"
#include "Components/SceneCaptureComponent2D.h"
#include "Components/SphereComponent.h"
#include "Engine/TextureRenderTarget2D.h"
#include "RMSimSdkSubsystem.h"
#include "Camera/PlayerCameraManager.h"
#include "GameFramework/FloatingPawnMovement.h"
#include "GameFramework/PlayerController.h"
#include "GameFramework/SpringArmComponent.h"

ARunePawn::ARunePawn()
{
	PrimaryActorTick.bCanEverTick = true;
	CollisionComponent = CreateDefaultSubobject<USphereComponent>(TEXT("Collision"));
	CollisionComponent->InitSphereRadius(16.0f);
	CollisionComponent->SetCollisionProfileName(TEXT("Pawn"));
	RootComponent = CollisionComponent;

	MovementComponent = CreateDefaultSubobject<UFloatingPawnMovement>(TEXT("Movement"));
	MovementComponent->MaxSpeed = 1200.0f;
	MovementComponent->Acceleration = 4000.0f;
	MovementComponent->Deceleration = 8000.0f;

	SpringArmComponent = CreateDefaultSubobject<USpringArmComponent>(TEXT("SpringArm"));
	SpringArmComponent->SetupAttachment(RootComponent);
	SpringArmComponent->TargetArmLength = 0.0f;
	SpringArmComponent->bUsePawnControlRotation = true;
	SpringArmComponent->bDoCollisionTest = false;

	CameraComponent = CreateDefaultSubobject<UCameraComponent>(TEXT("Camera"));
	CameraComponent->SetupAttachment(SpringArmComponent, USpringArmComponent::SocketName);
	CameraComponent->bUsePawnControlRotation = false;

	SdkCaptureComponent = CreateDefaultSubobject<USceneCaptureComponent2D>(TEXT("SdkCapture"));
	SdkCaptureComponent->SetupAttachment(CameraComponent);
	SdkCaptureComponent->CaptureSource = SCS_FinalColorLDR;
	SdkCaptureComponent->bCaptureEveryFrame = false;
	SdkCaptureComponent->bCaptureOnMovement = false;
	SdkCaptureComponent->bAlwaysPersistRenderingState = true;
}

void ARunePawn::BeginPlay()
{
	Super::BeginPlay();
	SdkRenderTarget = NewObject<UTextureRenderTarget2D>(this, TEXT("RMSimSdkRenderTarget"));
	SdkRenderTarget->RenderTargetFormat = RTF_RGBA8_SRGB;
	SdkRenderTarget->bForceLinearGamma = false;
	SdkRenderTarget->InitAutoFormat(RMSimSdk::ImageWidth, RMSimSdk::ImageHeight);
	SdkRenderTarget->UpdateResourceImmediate(true);
	SdkCaptureComponent->TextureTarget = SdkRenderTarget;
	SdkCaptureComponent->DetachFromComponent(FDetachmentTransformRules::KeepWorldTransform);
}

void ARunePawn::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	TimeSinceSdkCapture += DeltaSeconds;
	if (TimeSinceSdkCapture < SdkCaptureInterval || !SdkRenderTarget) return;
	TimeSinceSdkCapture = FMath::Fmod(TimeSinceSdkCapture, SdkCaptureInterval);
	if (APlayerController* PC = GetWorld()->GetFirstPlayerController())
	{
		if (APlayerCameraManager* CameraManager = PC->PlayerCameraManager)
		{
			const FMinimalViewInfo& View = CameraManager->GetCameraCacheView();
			SdkCaptureComponent->SetCameraView(View);
			SdkCaptureComponent->PostProcessSettings = View.PostProcessSettings;
			SdkCaptureComponent->PostProcessBlendWeight = View.PostProcessBlendWeight;
		}
	}
	SdkCaptureComponent->CaptureScene();
	TArray<FColor> Pixels;
	FTextureRenderTargetResource* Resource = SdkRenderTarget->GameThread_GetRenderTargetResource();
	if (Resource && Resource->ReadPixels(Pixels))
	{
		TArray<uint8> BGRA; BGRA.SetNumUninitialized(Pixels.Num() * sizeof(FColor));
		FMemory::Memcpy(BGRA.GetData(), Pixels.GetData(), BGRA.Num());
		if (URMSimSdkSubsystem* Sdk = GetWorld()->GetSubsystem<URMSimSdkSubsystem>()) Sdk->PublishCameraBGRA(BGRA, RMSimSdk::ImageWidth, RMSimSdk::ImageHeight, URMSimSdkSubsystem::NowNs());
	}
}

void ARunePawn::SetupPlayerInputComponent(UInputComponent* PlayerInputComponent)
{
	Super::SetupPlayerInputComponent(PlayerInputComponent);
	PlayerInputComponent->BindAxis(TEXT("MoveForward"), this, &ARunePawn::MoveForward);
	PlayerInputComponent->BindAxis(TEXT("MoveRight"), this, &ARunePawn::MoveRight);
	PlayerInputComponent->BindAxis(TEXT("MoveUp"), this, &ARunePawn::MoveUp);
}

void ARunePawn::MoveForward(float Value)
{
	if (Controller && !FMath::IsNearlyZero(Value))
	{
		AddMovementInput(Controller->GetControlRotation().Vector(), Value);
	}
}

void ARunePawn::MoveRight(float Value)
{
	if (Controller && !FMath::IsNearlyZero(Value))
	{
		const FVector Direction = FRotationMatrix(Controller->GetControlRotation()).GetUnitAxis(EAxis::Y);
		AddMovementInput(Direction, Value);
	}
}

void ARunePawn::MoveUp(float Value)
{
	AddMovementInput(FVector::UpVector, Value);
}

void ARunePawn::Turn(float Value)
{
	AddControllerYawInput(Value * MouseSensitivity);
}

void ARunePawn::LookUp(float Value)
{
	AddControllerPitchInput(Value * MouseSensitivity);
}
