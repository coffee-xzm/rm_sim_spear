#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Pawn.h"
#include "RunePawn.generated.h"

class UCameraComponent;
class USceneCaptureComponent2D;
class UTextureRenderTarget2D;
class UFloatingPawnMovement;
class USphereComponent;
class USpringArmComponent;

UCLASS()
class SIM_API ARunePawn : public APawn
{
	GENERATED_BODY()

public:
	ARunePawn();
	virtual void BeginPlay() override;
	virtual void Tick(float DeltaSeconds) override;
	virtual void SetupPlayerInputComponent(UInputComponent* PlayerInputComponent) override;

protected:
	void MoveForward(float Value);
	void MoveRight(float Value);
	void MoveUp(float Value);
	void Turn(float Value);
	void LookUp(float Value);

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Movement")
	TObjectPtr<USphereComponent> CollisionComponent;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Movement")
	TObjectPtr<UFloatingPawnMovement> MovementComponent;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Camera")
	TObjectPtr<USpringArmComponent> SpringArmComponent;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Camera")
	TObjectPtr<UCameraComponent> CameraComponent;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Camera")
	TObjectPtr<USceneCaptureComponent2D> SdkCaptureComponent;

	UPROPERTY(Transient)
	TObjectPtr<UTextureRenderTarget2D> SdkRenderTarget;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
	double MouseSensitivity = 1.0;
	double SdkCaptureInterval = 1.0 / 120.0;
	double TimeSinceSdkCapture = 0.0;
};
