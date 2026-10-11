#include "RunePlayerController.h"

#include "Blueprint/UserWidget.h"
#include "Camera/CameraComponent.h"
#include "Camera/PlayerCameraManager.h"
#include "RMSimWorldSubsystem.h"
#include "RMSimSdkSubsystem.h"
#include "RuneMenuWidget.h"
#include "RuneProjectile.h"
#include "RuneSceneDirector.h"

ARunePlayerController::ARunePlayerController()
{
	ProjectileClass = ARuneProjectile::StaticClass();
	bShowMouseCursor = false;
}

FTransform ARunePlayerController::ComposeShotTransform(
	const FTransform& CameraWorldTransform,
	const FTransform& InShotFrameFromCamera)
{
	return InShotFrameFromCamera * CameraWorldTransform;
}

void ARunePlayerController::BeginPlay()
{
	Super::BeginPlay();
	if (PlayerCameraManager)
	{
		PlayerCameraManager->ViewPitchMin = MinimumPitchDegrees;
		PlayerCameraManager->ViewPitchMax = MaximumPitchDegrees;
	}
	SetUIMode(false);
	// Establish the free camera target explicitly so mouse look is active from
	// the first frame, including when the map has no preconfigured view target.
	SetCameraView(ECameraView::FreeThirdPerson);
}

void ARunePlayerController::SetupInputComponent()
{
	Super::SetupInputComponent();
	InputComponent->BindAction(TEXT("ToggleMenu"), IE_Pressed, this, &ARunePlayerController::ToggleMenu);
	InputComponent->BindAction(TEXT("Fire"), IE_Pressed, this, &ARunePlayerController::Fire);
	InputComponent->BindKey(EKeys::RightMouseButton, IE_Pressed, this, &ARunePlayerController::ToggleCameraFollow);
	InputComponent->BindAxis(TEXT("Turn"), this, &ARunePlayerController::Turn);
	InputComponent->BindAxis(TEXT("LookUp"), this, &ARunePlayerController::LookUp);
}

void ARunePlayerController::Turn(float Value)
{
	if (!bUIMode && bCameraFollow)
	{
		AddYawInput(Value);
	}
}

void ARunePlayerController::LookUp(float Value)
{
	if (!bUIMode && bCameraFollow)
	{
		AddPitchInput(Value);
	}
}

void ARunePlayerController::PlayerTick(float DeltaTime)
{
	Super::PlayerTick(DeltaTime);
	if (URMSimSdkSubsystem* Sdk = GetWorld() ? GetWorld()->GetSubsystem<URMSimSdkSubsystem>() : nullptr)
	{
		RMSimSdk::FCommand Command;
		if (Sdk->ConsumeCommand(Command))
		{
			SetControlRotation(FRotator(Command.Pitch, Command.Yaw, Command.Roll));
			bCameraFollow = Command.bCameraFollow != 0;
		}
	}
}

bool ARunePlayerController::SetCameraView(ECameraView NewView)
{
	AActor* NewTarget = GetPawn();

	if (!NewTarget)
	{
		return false;
	}
	SetViewTargetWithBlend(NewTarget, 0.25f);
	ApplyCameraProjection(NewView);
	CurrentCameraView = NewView;
	// Both projection presets are movable. This flag now only controls whether
	// local mouse/controller input is accepted, not which camera actor is used.
	bCameraFollow = true;
	return true;
}

void ARunePlayerController::ApplyCameraProjection(ECameraView NewView)
{
	APawn* Pawn = GetPawn();
	UCameraComponent* PawnCamera = Pawn ? Pawn->FindComponentByClass<UCameraComponent>() : nullptr;
	if (!PawnCamera)
	{
		return;
	}

	if (!bFreeCameraProjectionCached)
	{
		FreeProjectionMode = PawnCamera->ProjectionMode;
		FreeFieldOfView = PawnCamera->FieldOfView;
		FreeOrthoWidth = PawnCamera->OrthoWidth;
		bFreeCameraProjectionCached = true;
	}

	if (NewView == ECameraView::FreeThirdPerson)
	{
		PawnCamera->ProjectionMode = FreeProjectionMode;
		PawnCamera->SetFieldOfView(FreeFieldOfView);
		PawnCamera->SetOrthoWidth(FreeOrthoWidth);
		return;
	}

	UCameraComponent* ReferenceCamera = nullptr;
	if (UWorld* World = GetWorld())
	{
		if (URMSimWorldSubsystem* Subsystem = World->GetSubsystem<URMSimWorldSubsystem>())
		{
			if (ARuneSceneDirector* Director = Subsystem->GetDirector())
			{
				ReferenceCamera = Director->FixedCamera
					? Director->FixedCamera->FindComponentByClass<UCameraComponent>() : nullptr;
			}
		}
	}
	if (!ReferenceCamera)
	{
		return;
	}

	PawnCamera->ProjectionMode = ReferenceCamera->ProjectionMode;
	PawnCamera->SetFieldOfView(ReferenceCamera->FieldOfView);
	PawnCamera->SetOrthoWidth(ReferenceCamera->OrthoWidth);
}

void ARunePlayerController::SetUIMode(bool bInUIMode)
{
	bUIMode = bInUIMode;
	bShowMouseCursor = bUIMode;
	SetIgnoreMoveInput(bUIMode);
	SetIgnoreLookInput(bUIMode);

	if (bUIMode)
	{
		if (!MenuWidgetInstance)
		{
			MenuWidgetInstance = CreateWidget<URuneMenuWidget>(this, URuneMenuWidget::StaticClass());
		}
		if (MenuWidgetInstance && !MenuWidgetInstance->IsInViewport())
		{
			MenuWidgetInstance->AddToViewport(100);
		}
		FInputModeGameAndUI Mode;
		if (MenuWidgetInstance)
		{
			Mode.SetWidgetToFocus(MenuWidgetInstance->TakeWidget());
		}
		Mode.SetHideCursorDuringCapture(false);
		SetInputMode(Mode);
	}
	else
	{
		if (MenuWidgetInstance)
		{
			MenuWidgetInstance->RemoveFromParent();
		}
		FInputModeGameOnly Mode;
		Mode.SetConsumeCaptureMouseDown(true);
		SetInputMode(Mode);
	}
}

void ARunePlayerController::ToggleMenu()
{
	SetUIMode(!bUIMode);
}

void ARunePlayerController::ToggleCameraFollow()
{
	bCameraFollow = !bCameraFollow;
}

void ARunePlayerController::Fire()
{
	UWorld* World = GetWorld();
	if (!World || bUIMode || !ProjectileClass)
	{
		return;
	}
	const double Now = World->GetTimeSeconds();
	if (Now - LastFireTimeSeconds < MinimumFireIntervalSeconds)
	{
		return;
	}
	LastFireTimeSeconds = Now;

	FVector CameraLocation;
	FRotator CameraRotation;
	GetPlayerViewPoint(CameraLocation, CameraRotation);
	const FTransform CameraTransform(CameraRotation, CameraLocation);
	const FTransform SpawnTransform = ComposeShotTransform(CameraTransform, ShotFrameFromCamera);
	FActorSpawnParameters SpawnParameters;
	SpawnParameters.Owner = this;
	SpawnParameters.Instigator = GetPawn();
	SpawnParameters.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	if (ARuneProjectile* Projectile = World->SpawnActor<ARuneProjectile>(ProjectileClass, SpawnTransform, SpawnParameters))
	{
		Projectile->SetShooterTeam(ShooterTeam);
	}
}
