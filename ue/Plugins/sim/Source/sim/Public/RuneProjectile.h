#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "RuneTypes.h"
#include "RuneProjectile.generated.h"

class UPointLightComponent;
class UProjectileMovementComponent;
class USphereComponent;
class UStaticMeshComponent;

UCLASS()
class SIM_API ARuneProjectile : public AActor
{
	GENERATED_BODY()

public:
	ARuneProjectile();

	void SetShooterTeam(ETeamColor InTeam) { ShooterTeam = InTeam; }
	ETeamColor GetShooterTeam() const { return ShooterTeam; }
	bool HasProcessedHit() const { return bProcessedHit; }

protected:
	UFUNCTION()
	void OnHit(UPrimitiveComponent* HitComponent, AActor* OtherActor,
		UPrimitiveComponent* OtherComp, FVector NormalImpulse, const FHitResult& Hit);

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Projectile")
	TObjectPtr<USphereComponent> CollisionComponent;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Projectile")
	TObjectPtr<UProjectileMovementComponent> MovementComponent;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Projectile")
	TObjectPtr<UStaticMeshComponent> VisualMesh;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Projectile")
	TObjectPtr<UPointLightComponent> GlowLight;

	UPROPERTY(EditDefaultsOnly, BlueprintReadOnly, Category = "Projectile")
	double InitialSpeed = 1500.0;

	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Projectile")
	ETeamColor ShooterTeam = ETeamColor::Red;

private:
	bool bProcessedHit = false;
};
