################################################################################
# Automatically-generated file. Do not edit!
# Toolchain: GNU Tools for STM32 (14.3.rel1)
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
C_SRCS += \
../Modules/Communication/NASCERR_Protocol.c \
../Modules/Communication/NASCERR_Registers.c 

OBJS += \
./Modules/Communication/NASCERR_Protocol.o \
./Modules/Communication/NASCERR_Registers.o 

C_DEPS += \
./Modules/Communication/NASCERR_Protocol.d \
./Modules/Communication/NASCERR_Registers.d 


# Each subdirectory must supply rules for building sources it contributes
Modules/Communication/%.o Modules/Communication/%.su Modules/Communication/%.cyclo: ../Modules/Communication/%.c Modules/Communication/subdir.mk
	arm-none-eabi-gcc "$<" -mcpu=cortex-m4 -std=gnu11 -g3 -DDEBUG -DUSE_HAL_DRIVER -DSTM32L4S5xx -c -I../Core/Inc -I../Modules/Communication -I../Modules/Database -I../Drivers/STM32L4xx_HAL_Driver/Inc -I../Drivers/STM32L4xx_HAL_Driver/Inc/Legacy -I../Drivers/CMSIS/Device/ST/STM32L4xx/Include -I../Drivers/CMSIS/Include -I../Modules/Bootloader -I../Modules/ExternalFlash -I../Modules/InternalFlash -I../Modules/System -I../Modules/Utils -I../Modules/Application -O0 -ffunction-sections -fdata-sections -Wall -fstack-usage -fcyclomatic-complexity -MMD -MP -MF"$(@:%.o=%.d)" -MT"$@" --specs=nano.specs -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -o "$@"

clean: clean-Modules-2f-Communication

clean-Modules-2f-Communication:
	-$(RM) ./Modules/Communication/NASCERR_Protocol.cyclo ./Modules/Communication/NASCERR_Protocol.d ./Modules/Communication/NASCERR_Protocol.o ./Modules/Communication/NASCERR_Protocol.su ./Modules/Communication/NASCERR_Registers.cyclo ./Modules/Communication/NASCERR_Registers.d ./Modules/Communication/NASCERR_Registers.o ./Modules/Communication/NASCERR_Registers.su

.PHONY: clean-Modules-2f-Communication

