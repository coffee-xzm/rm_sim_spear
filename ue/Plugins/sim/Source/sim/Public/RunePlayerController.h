#pragma once

#include "Camera/CameraTypes.h"
#include "CoreMinimal.h"
#include "GameFramework/PlayerController.h"
#include "RuneTypes.h"
#include "RMSimSdkProtocol.h"
#include "RunePlayerController.generated.h"

class ARuneProjectile;
class URuneMenuWidget;
class UCameraComponent;

UCLASS()
class SIM_API ARunePlayerController : public APlayerController
{
	GENERATED_BODY()

public:
	ARunePlayerController();

	virtual void BeginPlay() override;
	virtual void SetupInputComponent() override;
	virtual void PlayerTick(float DeltaTime) override;

	static FTransform ComposeShotTransform(const FTransform& CameraWorldTransform, const FTransform& ShotFrameFromCamera);

	UFUNCTION(BlueprintCallable, Category = "Rune|Camera")
	bool SetCameraView(ECameraView NewView);

	UFUNCTION(BlueprintCallable, Category = "Rune|UI")
	void SetUIMode(bool bInUIMode);

	UFUNCTION(BlueprintPure, Category = "Rune|UI")
	bool IsUIMode() const { return bUIMode; }

	UFUNCTION(BlueprintPure, Category = "Rune|Camera")
	ECameraView GetCameraView() const { return CurrentCameraView; }

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Rune|Projectile")
	ETeamColor ShooterTeam = ETeamColor::Red;

protected:
	UFUNCTION()
	void ToggleMenu();

	UFUNCTION()
	void Fire();
	void ToggleCameraFollow();
	void Turn(float Value);
	void LookUp(float Value);
	void ApplyCameraProjection(ECameraView NewView);

	UPROPERTY(EditDefaultsOnly, BlueprintReadOnly, Category = "Rune|Projectile")
	TSubclassOf<ARuneProjectile> ProjectileClass;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Rune|Projectile")
	FTransform ShotFrameFromCamera = FTransform(FRotator::ZeroRotator, FVector(0.0, 0.20, 0.0));

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Rune|Projectile")
	double MinimumFireIntervalSeconds = 0.1;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Rune|Camera")
	double MinimumPitchDegrees = -89.0;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Rune|Camera")
	double MaximumPitchDegrees = 89.0;

private:
	UPROPERTY()
	TObjectPtr<URuneMenuWidget> MenuWidgetInstance = nullptr;

	ECameraView CurrentCameraView = ECameraView::FreeThirdPerson;
	bool bUIMode = false;
	bool bCameraFollow = true;
	bool bFreeCameraProjectionCached = false;
	TEnumAsByte<ECameraProjectionMode::Type> FreeProjectionMode = ECameraProjectionMode::Perspective;
	double FreeFieldOfView = 90.0;
	double FreeOrthoWidth = 512.0;
	double LastFireTimeSeconds = -DBL_MAX;
};
